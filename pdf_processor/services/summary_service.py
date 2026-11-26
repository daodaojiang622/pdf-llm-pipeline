import os
import time
import requests
import logging
from dotenv import load_dotenv

# Set up logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Load environment variables from .env file (for local development)
if not os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    load_dotenv()

GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
DEFAULT_GEMINI_MODEL = "gemini-flash-lite-latest"
DEFAULT_GROK_API_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_GROK_MODEL = "grok-4.3"

DEFAULT_TIMEOUT_SECONDS = 90

# Roughly 50,000 tokens, well within current models' context windows
DEFAULT_MAX_CHARS = 200_000

# Free tiers rate-limit bursts, so retry 429 and 5xx responses briefly
RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_DELAYS_SECONDS = (2, 5)

SYSTEM_PROMPT = (
    "You are a helpful assistant that creates concise summaries of documents."
)
USER_PROMPT = (
    "Please provide a comprehensive summary of the following text extracted from a PDF. "
    "Focus on the main points, key findings, and important details.\n\nTEXT:\n{text}"
)


class SummaryError(Exception):
    """The summary could not be generated."""


def _mock_summary_allowed():
    """Placeholder summaries are for local development only.

    ALLOW_MOCK_SUMMARY overrides the default, which is on locally and off on Lambda.
    """
    value = os.environ.get("ALLOW_MOCK_SUMMARY")
    if value is None:
        return not os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    return value.lower() == "true"


def _fallback_or_raise(text, reason):
    """Return a placeholder summary if allowed, otherwise fail the job."""
    if _mock_summary_allowed():
        logger.warning(f"{reason}; returning a placeholder summary")
        return _generate_mock_summary(text)
    raise SummaryError(reason)


def _timeout_seconds():
    """Per-request timeout. Busy models can take well over 30 seconds to respond."""
    return float(os.environ.get("SUMMARY_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS)


def _post_with_retry(url, headers, payload):
    """POST, retrying briefly on rate limits and server errors."""
    for attempt, delay in enumerate((0,) + RETRY_DELAYS_SECONDS):
        if delay:
            logger.warning(f"LLM request retry {attempt} in {delay}s")
            time.sleep(delay)
        response = requests.post(
            url, headers=headers, json=payload, timeout=_timeout_seconds()
        )
        if response.status_code not in RETRY_STATUSES:
            break
    response.raise_for_status()
    return response.json()


def _summarize_with_gemini(text):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SummaryError("GEMINI_API_KEY is not configured")
    model = os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [
            {"role": "user", "parts": [{"text": USER_PROMPT.format(text=text)}]}
        ],
        "generationConfig": {"temperature": 0.3},
    }
    result = _post_with_retry(
        GEMINI_API_URL.format(model=model),
        {"Content-Type": "application/json", "x-goog-api-key": api_key},
        payload,
    )
    block_reason = (result.get("promptFeedback") or {}).get("blockReason")
    if block_reason:
        raise SummaryError(f"Gemini blocked the request: {block_reason}")
    candidates = result.get("candidates") or [{}]
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(part.get("text", "") for part in parts).strip()


def _summarize_with_grok(text):
    api_key = os.environ.get("GROK_API_KEY")
    if not api_key:
        raise SummaryError("GROK_API_KEY is not configured")
    payload = {
        "model": os.environ.get("GROK_MODEL") or DEFAULT_GROK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT.format(text=text)},
        ],
        "stream": False,
        "temperature": 0.3,
    }
    result = _post_with_retry(
        os.environ.get("GROK_API_URL") or DEFAULT_GROK_API_URL,
        {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        payload,
    )
    return (
        (result.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    )


PROVIDERS = {"gemini": _summarize_with_gemini, "grok": _summarize_with_grok}


def generate_summary(text: str):
    """
    Summarize text with the provider named by LLM_PROVIDER (default: gemini).

    Raises SummaryError on failure unless placeholder summaries are allowed.
    """
    max_chars = int(os.environ.get("SUMMARY_MAX_CHARS") or DEFAULT_MAX_CHARS)
    if len(text) > max_chars:
        logger.info(f"Text exceeds {max_chars} characters, truncating")
        text = text[:max_chars] + "..."

    provider = (os.environ.get("LLM_PROVIDER") or "gemini").lower()
    summarize = PROVIDERS.get(provider)
    if summarize is None:
        raise SummaryError(f"Unknown LLM_PROVIDER: {provider}")

    try:
        summary = summarize(text)
        if not summary:
            return _fallback_or_raise(text, f"{provider} returned an empty summary")
        logger.info(f"Successfully generated summary with {provider}")
        return summary

    except SummaryError as e:
        return _fallback_or_raise(text, str(e))

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        logger.error(
            f"{provider} HTTP error {status}: {e.response.text[:500] if e.response is not None else ''}"
        )
        return _fallback_or_raise(text, f"{provider} API returned HTTP {status}")

    except requests.exceptions.ConnectionError:
        return _fallback_or_raise(text, f"Could not connect to the {provider} API")

    except requests.exceptions.Timeout:
        return _fallback_or_raise(text, f"{provider} API request timed out")

    except Exception as e:
        logger.error(f"Error generating summary with {provider}: {type(e).__name__}")
        return _fallback_or_raise(text, f"Error generating summary: {type(e).__name__}")


def _generate_mock_summary(text):
    """
    Generate a simple summary without using an external API
    This is a fallback method for when the API is unavailable
    """
    logger.info("Using fallback mock summary generation")

    paragraphs = text.split("\n\n")
    summary_parts = []

    for para in paragraphs[:5]:
        sentences = para.split(". ")
        if sentences:
            summary_parts.append(sentences[0])

    summary = " ".join(summary_parts)

    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        env_note = "AWS Lambda"
    else:
        env_note = "local environment"

    summary += f"\n\n[Note: This is a basic extraction-based summary generated in {env_note}. For better results, configure an LLM API key.]"

    return summary
