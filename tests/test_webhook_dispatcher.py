import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import TCPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pdf_processor"))

from services import webhook_dispatcher as wd  # noqa: E402

SECRET = "test-secret"
URL = "https://hooks.example.com/pdf"


def payload(event_type=wd.EVENT_SUMMARIZED):
    return wd.build_payload(
        "doc-1", event_type, summary_url="https://files.example.com/s"
    )


class SignatureTests(unittest.TestCase):
    def test_known_signature_and_header_format(self):
        header = wd.sign_webhook_payload('{"a":1}', SECRET, timestamp=1700000000)
        self.assertEqual(
            header,
            "t=1700000000,v1=8cb2c3355fca388e9ac2caec004f4d5d7045d74937ab5faad61dc11682247a9f",
        )

    def test_valid_signature_verifies(self):
        header = wd.sign_webhook_payload("body", SECRET, timestamp=1000)
        self.assertTrue(wd.verify_webhook_signature("body", header, SECRET, now=1000))
        self.assertTrue(wd.verify_webhook_signature(b"body", header, SECRET, now=1300))

    def test_rejects_tampered_payload(self):
        header = wd.sign_webhook_payload("body", SECRET, timestamp=1000)
        self.assertFalse(wd.verify_webhook_signature("bodY", header, SECRET, now=1000))

    def test_rejects_wrong_secret(self):
        header = wd.sign_webhook_payload("body", SECRET, timestamp=1000)
        self.assertFalse(wd.verify_webhook_signature("body", header, "other", now=1000))

    def test_rejects_expired_and_future_timestamps(self):
        header = wd.sign_webhook_payload("body", SECRET, timestamp=1000)
        self.assertFalse(wd.verify_webhook_signature("body", header, SECRET, now=1301))
        self.assertFalse(wd.verify_webhook_signature("body", header, SECRET, now=699))

    def test_rejects_changed_timestamp(self):
        header = wd.sign_webhook_payload("body", SECRET, timestamp=1000)
        forged = header.replace("t=1000", "t=1001")
        self.assertFalse(wd.verify_webhook_signature("body", forged, SECRET, now=1001))

    def test_rejects_malformed_headers(self):
        for header in (
            "",
            "garbage",
            "t=abc,v1=00",
            "t=1000",
            "v1=00",
            "t=1000,v1=short",
            "t=1000,v1=é",
        ):
            self.assertFalse(
                wd.verify_webhook_signature("body", header, SECRET, now=1000), header
            )


class PayloadTests(unittest.TestCase):
    def test_event_id_is_stable_per_document_and_type(self):
        self.assertEqual(payload().event_id, payload().event_id)
        self.assertNotEqual(payload().event_id, payload(wd.EVENT_FAILED).event_id)

    def test_json_shape_omits_empty_fields(self):
        data = json.loads(
            wd.build_payload("doc-1", wd.EVENT_FAILED, error="failed").to_json()
        )
        self.assertEqual(
            set(data), {"eventId", "eventType", "documentId", "completedAt", "error"}
        )
        self.assertTrue(data["completedAt"].endswith("Z"))

    def test_rejects_unknown_event(self):
        with self.assertRaises(ValueError):
            wd.build_payload("doc-1", "document.unknown")


class UrlValidationTests(unittest.TestCase):
    def test_accepts_public_https(self):
        self.assertEqual(wd.validate_webhook_url(URL, allow_private=False), URL)

    def test_rejects_unsafe_urls(self):
        for url in (
            "http://example.com/hook",
            "ftp://example.com/hook",
            "https://localhost/hook",
            "https://127.0.0.1/hook",
            "https://10.0.0.5/hook",
            "https://169.254.169.254/latest/meta-data",
            "https://[::1]/hook",
            "https://[::ffff:169.254.169.254]/hook",
            "https://user:pass@example.com/hook",
            "https://example.com/a b",
            "https://example.com:99999/hook",
            "https://example.com/" + "a" * 2048,
        ):
            with self.assertRaises(wd.WebhookUrlError, msg=url):
                wd.validate_webhook_url(url, allow_private=False)

    def test_blocks_dns_name_resolving_to_private_ip(self):
        answer = [(2, 1, 6, "", ("169.254.169.254", 443))]
        with mock.patch.object(
            wd.socket, "getaddrinfo", return_value=answer
        ), mock.patch.object(wd.socket, "create_connection") as connect:
            with self.assertRaises(wd.WebhookUrlError):
                wd._connect_to_validated_address(("rebind.example.com", 443), 1, False)
        connect.assert_not_called()

    def test_connects_to_the_validated_ip(self):
        answer = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with mock.patch.object(
            wd.socket, "getaddrinfo", return_value=answer
        ), mock.patch.object(wd.socket, "create_connection") as connect:
            wd._connect_to_validated_address(("example.com", 443), 2, False)
        connect.assert_called_once_with(("93.184.216.34", 443), 2)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        for target, attribute in ((wd, "_post"), (wd.time, "sleep")):
            patcher = mock.patch.object(target, attribute)
            setattr(self, attribute.strip("_"), patcher.start())
            self.addCleanup(patcher.stop)
        random_patch = mock.patch.object(wd.random, "random", return_value=0.5)
        random_patch.start()
        self.addCleanup(random_patch.stop)

    def test_success_sends_signed_headers(self):
        self.post.return_value = 200
        item = payload()
        result = wd.dispatch_webhook(URL, item, SECRET)
        self.assertEqual(result, {"delivered": True, "status": 200, "attempts": 1})
        _, body, headers, timeout = self.post.call_args.args
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["X-Idempotency-Key"], item.event_id)
        self.assertEqual(headers["X-Webhook-Event"], item.event_type)
        self.assertTrue(
            wd.verify_webhook_signature(body, headers["X-Webhook-Signature"], SECRET)
        )
        self.assertEqual(timeout, 5)
        self.sleep.assert_not_called()

    def test_retries_retryable_statuses_then_succeeds(self):
        for status in (500, 503, 429, 408):
            self.post.reset_mock()
            self.post.side_effect = [status, 204]
            result = wd.dispatch_webhook(URL, payload(), SECRET)
            self.assertEqual(result["attempts"], 2, status)

    def test_retries_network_error_and_timeout(self):
        self.post.side_effect = [ConnectionResetError(), TimeoutError(), 200]
        self.assertEqual(wd.dispatch_webhook(URL, payload(), SECRET)["attempts"], 3)

    def test_fails_fast_on_non_retryable_statuses(self):
        for status in (400, 401, 403, 404, 422, 301):
            self.post.reset_mock()
            self.sleep.reset_mock()
            self.post.side_effect = None
            self.post.return_value = status
            with self.assertRaises(wd.WebhookDeliveryError) as caught:
                wd.dispatch_webhook(URL, payload(), SECRET)
            self.assertEqual(caught.exception.status, status)
            self.assertEqual(self.post.call_count, 1)
            self.sleep.assert_not_called()

    def test_raises_after_exhausting_retries(self):
        self.post.return_value = 500
        with self.assertRaises(wd.WebhookDeliveryError) as caught:
            wd.dispatch_webhook(URL, payload(), SECRET, max_retries=4)
        self.assertEqual(caught.exception.attempts, 5)
        self.assertEqual(self.post.call_count, 5)
        self.assertEqual(self.sleep.call_count, 4)

    def test_full_jitter_delay_formula(self):
        self.post.return_value = 500
        with self.assertRaises(wd.WebhookDeliveryError):
            wd.dispatch_webhook(
                URL, payload(), SECRET, max_retries=4, base_delay_ms=1000
            )
        delays = [call.args[0] for call in self.sleep.call_args_list]
        self.assertEqual(delays, [0.5, 1.0, 2.0, 4.0])

    def test_time_budget_stops_retries(self):
        self.post.return_value = 500
        with self.assertRaises(wd.WebhookDeliveryError):
            wd.dispatch_webhook(
                URL, payload(), SECRET, base_delay_ms=1000, time_budget_ms=100
            )
        self.assertEqual(self.post.call_count, 1)
        self.sleep.assert_not_called()

    def test_unsafe_url_is_never_requested(self):
        with self.assertRaises(wd.WebhookDeliveryError):
            wd.dispatch_webhook("https://169.254.169.254/", payload(), SECRET)
        self.post.assert_not_called()


class NotifyTests(unittest.TestCase):
    def test_no_url_is_a_noop(self):
        with mock.patch.dict(
            os.environ, {"WEBHOOK_SECRET": SECRET}, clear=True
        ), mock.patch.object(wd, "dispatch_webhook") as dispatch:
            self.assertFalse(wd.notify_job_outcome("job", wd.EVENT_SUMMARIZED))
        dispatch.assert_not_called()

    def test_missing_secret_never_sends_unsigned(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            wd, "dispatch_webhook"
        ) as dispatch, self.assertLogs(wd.logger, "ERROR"):
            self.assertFalse(wd.notify_job_outcome("job", wd.EVENT_FAILED, URL))
        dispatch.assert_not_called()

    def test_uses_default_url(self):
        env = {"WEBHOOK_SECRET": SECRET, "DEFAULT_WEBHOOK_URL": URL}
        result = {"delivered": True, "status": 200, "attempts": 1}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            wd, "dispatch_webhook", return_value=result
        ) as dispatch:
            self.assertTrue(
                wd.notify_job_outcome("job", wd.EVENT_FAILED, error="failed")
            )
        self.assertEqual(dispatch.call_args.args[0], URL)

    def test_delivery_failure_does_not_raise(self):
        error = wd.WebhookDeliveryError("down", attempts=5, status=500)
        with mock.patch.dict(
            os.environ, {"WEBHOOK_SECRET": SECRET}, clear=True
        ), mock.patch.object(
            wd, "dispatch_webhook", side_effect=error
        ), self.assertLogs(
            wd.logger, "ERROR"
        ) as logs:
            self.assertFalse(wd.notify_job_outcome("job", wd.EVENT_SUMMARIZED, URL))
        self.assertNotIn(URL, "".join(logs.output))

    def test_presign_failure_still_notifies(self):
        result = {"delivered": True, "status": 200, "attempts": 1}
        with mock.patch.dict(
            os.environ, {"WEBHOOK_SECRET": SECRET}, clear=True
        ), mock.patch.object(
            wd, "_presign_summary_url", side_effect=RuntimeError("no creds")
        ), mock.patch.object(
            wd, "dispatch_webhook", return_value=result
        ) as dispatch, self.assertLogs(
            wd.logger, "ERROR"
        ):
            self.assertTrue(
                wd.notify_job_outcome(
                    "job", wd.EVENT_SUMMARIZED, URL, summary_path="s3://b/k"
                )
            )
        self.assertIsNone(dispatch.call_args.args[1].summary_url)

    def test_hides_webhook_url_from_api_output(self):
        self.assertEqual(
            wd.without_webhook_url({"status": "ok", "webhook_url": URL}),
            {"status": "ok"},
        )


class LocalHTTPServer(HTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind does a reverse DNS lookup that can stall tests.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class RealHttpTests(unittest.TestCase):
    """Exercises the real socket path against a local server."""

    def setUp(self):
        test = self
        self.responses = [503, 200]
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                test.requests.append((self.path, dict(self.headers), body))
                self.send_response(test.responses.pop(0))
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = LocalHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_delivers_verifiable_request_after_retry(self):
        url = f"http://127.0.0.1:{self.server.server_port}/hook?source=test"
        with mock.patch.dict(os.environ, {"WEBHOOK_ALLOW_PRIVATE_URLS": "true"}):
            result = wd.dispatch_webhook(url, payload(), SECRET, base_delay_ms=1)
        self.assertEqual(result["attempts"], 2)
        path, headers, body = self.requests[-1]
        self.assertEqual(path, "/hook?source=test")
        self.assertTrue(
            wd.verify_webhook_signature(body, headers["X-Webhook-Signature"], SECRET)
        )


if __name__ == "__main__":
    unittest.main()
