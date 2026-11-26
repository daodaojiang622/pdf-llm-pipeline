"""Failure handling for the extraction and summary steps.

Extraction tests run the real PyMuPDF fallback against the committed samples,
with MinerU stubbed out, so they need PyMuPDF installed (it is in the image).
"""

import importlib
import importlib.util
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SAMPLES = os.path.join(ROOT, "samples")
sys.path.insert(0, os.path.join(ROOT, "pdf_processor"))

HAS_FITZ = importlib.util.find_spec("fitz") is not None
HAS_SUMMARY_DEPS = all(importlib.util.find_spec(m) for m in ("requests", "dotenv"))
SUMMARY_ENV_KEYS = (
    "ALLOW_MOCK_SUMMARY",
    "AWS_LAMBDA_FUNCTION_NAME",
    "LLM_PROVIDER",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "GROK_API_KEY",
    "GROK_API_URL",
    "GROK_MODEL",
    "SUMMARY_MAX_CHARS",
)


def load_ocr_service():
    """Import ocr_service with MinerU failing, so the PyMuPDF fallback runs."""
    magic = mock.MagicMock()
    magic.PymuDocDataset.side_effect = RuntimeError("MinerU unavailable in tests")
    stubs = {
        name: magic
        for name in (
            "magic_pdf",
            "magic_pdf.data",
            "magic_pdf.data.data_reader_writer",
            "magic_pdf.data.dataset",
            "magic_pdf.model",
            "magic_pdf.model.doc_analyze_by_custom_model",
            "magic_pdf.config",
            "magic_pdf.config.enums",
        )
    }
    stubs["services.storage_service"] = types.SimpleNamespace(
        download_to_temp=lambda path: path, write_file=mock.Mock()
    )
    with mock.patch.dict(sys.modules, stubs):
        sys.modules.pop("services.ocr_service", None)
        module = importlib.import_module("services.ocr_service")
    sys.modules.pop("services.ocr_service", None)
    return module


def sample(*parts):
    return os.path.join(SAMPLES, *parts)


@unittest.skipUnless(HAS_FITZ, "PyMuPDF is not installed")
class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.ocr = load_ocr_service()
        quiet = mock.patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)

    def test_text_pdf_returns_text(self):
        text = self.ocr.extract_text_from_pdf(sample("irs-w9", "fw9.pdf"))
        self.assertIn("W-9", text)
        self.assertGreater(len(text), 1000)

    def test_scanned_pdf_without_ocr_raises(self):
        with self.assertRaises(self.ocr.ExtractionError) as caught:
            self.ocr.extract_text_from_pdf(sample("generated", "scanned-fw9-p1-3.pdf"))
        self.assertIn("No extractable text", str(caught.exception))

    def test_unreadable_files_raise(self):
        for name in ("encrypted.pdf", "not-a-pdf.pdf", "empty.pdf"):
            with self.subTest(name=name):
                with self.assertRaises(self.ocr.ExtractionError) as caught:
                    self.ocr.extract_text_from_pdf(sample("generated", name))
                self.assertIn("Could not read the PDF", str(caught.exception))

    def test_formula_recognition_is_off_by_default(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("MINERU_FORMULA_ENABLE", None)
            self.assertFalse(self.ocr._formula_enabled())
            os.environ["MINERU_FORMULA_ENABLE"] = "true"
            self.assertTrue(self.ocr._formula_enabled())

    def test_nearly_empty_text_raises(self):
        with mock.patch.object(self.ocr, "_extract_text", return_value="  \n 12 \n "):
            with self.assertRaises(self.ocr.ExtractionError):
                self.ocr.extract_text_from_pdf("unused.pdf")


@unittest.skipUnless(HAS_SUMMARY_DEPS, "requests or python-dotenv is not installed")
class SummaryTests(unittest.TestCase):
    def setUp(self):
        sys.modules.pop("services.summary_service", None)
        self.summary = importlib.import_module("services.summary_service")
        self.addCleanup(sys.modules.pop, "services.summary_service", None)
        sleep = mock.patch.object(self.summary.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def env(self, **values):
        """Clear every summary-related variable, then set the given ones."""
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in SUMMARY_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(values)

    def response(self, status=200, content="A real summary.", body=None):
        result = mock.Mock(status_code=status, text="error body")
        result.json.return_value = body or {
            "candidates": [{"content": {"parts": [{"text": content}]}}]
        }
        if status >= 400:
            error = self.summary.requests.exceptions.HTTPError(
                f"{status} error", response=result
            )
            result.raise_for_status.side_effect = error
        return result

    def post(self, *responses):
        patcher = mock.patch.object(
            self.summary.requests, "post", side_effect=responses
        )
        post = patcher.start()
        self.addCleanup(patcher.stop)
        return post

    def test_gemini_is_default_and_key_stays_out_of_url(self):
        self.env(GEMINI_API_KEY="test-key", GEMINI_MODEL="gemini-test")
        post = self.post(self.response())
        self.assertEqual(self.summary.generate_summary("Some text."), "A real summary.")
        url = post.call_args.args[0]
        self.assertIn("models/gemini-test:generateContent", url)
        self.assertNotIn("test-key", url)
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "test-key")

    def test_grok_provider(self):
        self.env(LLM_PROVIDER="grok", GROK_API_KEY="test-key")
        body = {"choices": [{"message": {"content": "Grok summary."}}]}
        post = self.post(self.response(body=body))
        self.assertEqual(self.summary.generate_summary("Some text."), "Grok summary.")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "grok-4.3")

    def test_unknown_provider_raises(self):
        self.env(LLM_PROVIDER="other", ALLOW_MOCK_SUMMARY="true")
        with self.assertRaises(self.summary.SummaryError):
            self.summary.generate_summary("Some text.")

    def test_missing_key_raises_when_mock_disallowed(self):
        self.env(ALLOW_MOCK_SUMMARY="false")
        with self.assertRaises(self.summary.SummaryError) as caught:
            self.summary.generate_summary("Some extracted text.")
        self.assertIn("GEMINI_API_KEY", str(caught.exception))

    def test_missing_key_returns_placeholder_when_allowed(self):
        self.env(ALLOW_MOCK_SUMMARY="true")
        self.assertIn("[Note:", self.summary.generate_summary("Some extracted text."))

    def test_defaults_fail_on_lambda_and_mock_locally(self):
        self.env(AWS_LAMBDA_FUNCTION_NAME="pdf-processor")
        with self.assertRaises(self.summary.SummaryError):
            self.summary.generate_summary("Some extracted text.")
        os.environ.pop("AWS_LAMBDA_FUNCTION_NAME")
        self.assertIn("[Note:", self.summary.generate_summary("Some extracted text."))

    def test_rate_limit_is_retried_then_succeeds(self):
        self.env(GEMINI_API_KEY="test-key", ALLOW_MOCK_SUMMARY="false")
        post = self.post(self.response(429), self.response())
        self.assertEqual(self.summary.generate_summary("Some text."), "A real summary.")
        self.assertEqual(post.call_count, 2)
        self.sleep.assert_called_once_with(2)

    def test_persistent_rate_limit_raises(self):
        self.env(GEMINI_API_KEY="test-key", ALLOW_MOCK_SUMMARY="false")
        post = self.post(*(self.response(429) for _ in range(3)))
        with self.assertRaises(self.summary.SummaryError) as caught:
            self.summary.generate_summary("Some text.")
        self.assertIn("429", str(caught.exception))
        self.assertEqual(post.call_count, 3)

    def test_client_error_is_not_retried(self):
        self.env(GEMINI_API_KEY="test-key", ALLOW_MOCK_SUMMARY="false")
        post = self.post(self.response(404))
        with self.assertRaises(self.summary.SummaryError) as caught:
            self.summary.generate_summary("Some text.")
        self.assertIn("404", str(caught.exception))
        self.assertNotIn("test-key", str(caught.exception))
        self.assertEqual(post.call_count, 1)

    def test_connection_error_raises_when_mock_disallowed(self):
        self.env(GEMINI_API_KEY="test-key", ALLOW_MOCK_SUMMARY="false")
        self.post(self.summary.requests.exceptions.ConnectionError("down"))
        with self.assertRaises(self.summary.SummaryError):
            self.summary.generate_summary("Some text.")

    def test_blocked_or_empty_response_raises(self):
        self.env(GEMINI_API_KEY="test-key", ALLOW_MOCK_SUMMARY="false")
        blocked = {"promptFeedback": {"blockReason": "SAFETY"}}
        self.post(self.response(body=blocked), self.response(content="  "))
        with self.assertRaises(self.summary.SummaryError) as caught:
            self.summary.generate_summary("Some text.")
        self.assertIn("SAFETY", str(caught.exception))
        with self.assertRaises(self.summary.SummaryError):
            self.summary.generate_summary("Some text.")

    def test_long_text_is_truncated(self):
        self.env(GEMINI_API_KEY="test-key", SUMMARY_MAX_CHARS="50")
        post = self.post(self.response())
        self.summary.generate_summary("x" * 500)
        prompt = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("x" * 50 + "...", prompt)
        self.assertNotIn("x" * 51, prompt)


if __name__ == "__main__":
    unittest.main()
