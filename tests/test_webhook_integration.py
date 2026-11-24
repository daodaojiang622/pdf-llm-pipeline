"""Runs the Lambda worker with AWS and the OCR/LLM services stubbed."""

import importlib
import json
import os
from decimal import Decimal
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "pdf_processor"))

URL = "https://hooks.example.com/pdf?token=private"


def load_handler():
    stubs = {
        "boto3": mock.MagicMock(),
        "services.ocr_service": types.SimpleNamespace(
            extract_text_from_pdf=mock.Mock()
        ),
        "services.summary_service": types.SimpleNamespace(generate_summary=mock.Mock()),
        "services.storage_service": types.SimpleNamespace(save_file=mock.Mock()),
    }
    with mock.patch.dict(sys.modules, stubs):
        sys.modules.pop("lambda_handler", None)
        module = importlib.import_module("lambda_handler")
    sys.modules.pop("lambda_handler", None)
    return module


def s3_event(job_id="job-1"):
    key = f"uploads/{job_id}/{job_id}.pdf"
    return {"Records": [{"s3": {"bucket": {"name": "pdfs"}, "object": {"key": key}}}]}


class LambdaWorkerTests(unittest.TestCase):
    def setUp(self):
        self.handler = load_handler()
        self.handler.s3 = mock.MagicMock()
        self.handler.jobs_table = mock.MagicMock()
        self.handler.jobs_table.get_item.return_value = {
            "Item": {"job_id": "job-1", "status": "completed", "webhook_url": URL}
        }
        self.handler.extract_text_from_pdf = mock.Mock(return_value="text")
        self.handler.generate_summary = mock.Mock(return_value="summary")
        self.context = mock.Mock()
        self.context.get_remaining_time_in_millis.return_value = 60000

    def statuses(self):
        return [
            call.kwargs["ExpressionAttributeValues"][":status"]
            for call in self.handler.jobs_table.update_item.call_args_list
        ]

    def test_success_sends_summarized_event(self):
        with mock.patch.object(self.handler, "notify_job_outcome") as notify:
            response = self.handler.handle_s3_event(s3_event(), self.context)
        self.assertEqual(response["statusCode"], 200)
        args, kwargs = notify.call_args
        self.assertEqual(args, ("job-1", "document.summarized"))
        self.assertEqual(kwargs["webhook_url"], URL)
        self.assertEqual(kwargs["time_budget_ms"], 55000)
        self.assertTrue(kwargs["summary_path"].endswith("job-1_summary.txt"))

    def test_webhook_crash_does_not_fail_saved_job(self):
        with mock.patch.object(
            self.handler, "notify_job_outcome", side_effect=RuntimeError
        ):
            response = self.handler.handle_s3_event(s3_event(), self.context)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(self.statuses()[-1], "completed")
        self.assertNotIn("failed", self.statuses())

    def test_failure_sends_sanitized_event_and_reraises(self):
        self.handler.generate_summary.side_effect = RuntimeError(
            "secret internal detail"
        )
        with mock.patch.object(self.handler, "notify_job_outcome") as notify:
            with self.assertRaises(RuntimeError):
                self.handler.handle_s3_event(s3_event(), self.context)
        self.assertEqual(self.statuses()[-1], "failed")
        self.assertEqual(notify.call_args.args[1], "document.failed")
        self.assertNotIn("secret", notify.call_args.kwargs["error"])

    def test_upload_persists_webhook_url(self):
        event = {"body": "pdf", "headers": {"x-webhook-url": URL}}
        response = self.handler.handle_pdf_upload(event)
        self.assertEqual(response["statusCode"], 200)
        item = self.handler.jobs_table.put_item.call_args.kwargs["Item"]
        self.assertEqual(item["webhook_url"], URL)

    def test_upload_rejects_unsafe_url_before_storing(self):
        event = {
            "body": "pdf",
            "headers": {"X-Webhook-Url": "https://169.254.169.254/"},
        }
        response = self.handler.handle_pdf_upload(event)
        self.assertEqual(response["statusCode"], 400)
        self.handler.s3.put_object.assert_not_called()
        self.handler.jobs_table.put_item.assert_not_called()

    def test_status_response_hides_webhook_url(self):
        response = self.handler.get_job_status("job-1")
        self.assertNotIn("webhook_url", json.loads(response["body"]))

    def test_event_log_redacts_webhook_url(self):
        event = {"headers": {"X-Webhook-Url": URL, "accept": "*/*"}}
        self.assertNotIn(
            "private", json.dumps(self.handler.redact_event_for_logging(event))
        )
        self.assertEqual(event["headers"]["X-Webhook-Url"], URL)

    def test_status_serializes_dynamodb_numbers(self):
        self.handler.jobs_table.get_item.return_value = {
            "Item": {
                "job_id": "job-1",
                "created_at": Decimal("1700000000"),
                "score": Decimal("1.5"),
            }
        }
        response = self.handler.get_job_status("job-1")
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["created_at"], 1700000000)
        self.assertEqual(body["score"], 1.5)

    def test_upload_creates_job_before_s3_object(self):
        order = mock.Mock()
        order.attach_mock(self.handler.jobs_table.put_item, "put_item")
        order.attach_mock(self.handler.s3.put_object, "put_object")
        self.handler.handle_pdf_upload({"body": "pdf", "headers": {}})
        self.assertEqual(
            [call[0] for call in order.mock_calls], ["put_item", "put_object"]
        )

    def test_failed_s3_upload_removes_job(self):
        self.handler.s3.put_object.side_effect = RuntimeError("s3 down")
        with self.assertRaises(RuntimeError):
            self.handler.handle_pdf_upload({"body": "pdf", "headers": {}})
        self.handler.jobs_table.delete_item.assert_called_once()

    def test_s3_key_is_url_decoded(self):
        event = s3_event()
        event["Records"][0]["s3"]["object"]["key"] = "uploads/job-1/my+file%281%29.pdf"
        with mock.patch.object(self.handler, "notify_job_outcome"):
            self.handler.handle_s3_event(event, self.context)
        self.handler.s3.download_file.assert_called_once()
        self.assertEqual(
            self.handler.s3.download_file.call_args.args[1],
            "uploads/job-1/my file(1).pdf",
        )


if __name__ == "__main__":
    unittest.main()
