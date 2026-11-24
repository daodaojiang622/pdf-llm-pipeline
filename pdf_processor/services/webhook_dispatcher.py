"""Signed outbound webhooks with retries, using only the standard library.

Compatible with Python 3.9, the Lambda runtime used by this project.
"""

import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import os
import random
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Union
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

EVENT_SUMMARIZED = "document.summarized"
EVENT_FAILED = "document.failed"
EVENT_TYPES = (EVENT_SUMMARIZED, EVENT_FAILED)

SIGNATURE_TOLERANCE_SECONDS = 300
MAX_WEBHOOK_URL_LENGTH = 2048
SUMMARY_URL_EXPIRY_SECONDS = 3600

# Stable namespace so redelivery of one job outcome keeps the same eventId.
_EVENT_NAMESPACE = uuid.UUID("6f1d2c1e-8f0b-5a57-9a43-0c7f6f6f7b11")


class WebhookUrlError(ValueError):
    """The webhook URL is not an allowed delivery target."""


class WebhookDeliveryError(Exception):
    """Delivery failed permanently or retries were exhausted."""

    def __init__(self, message, attempts, status=None):
        super().__init__(message)
        self.attempts = attempts
        self.status = status


@dataclass(frozen=True)
class WebhookPayload:
    event_id: str
    event_type: str
    document_id: str
    completed_at: str
    summary_url: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self):
        data = {
            "eventId": self.event_id,
            "eventType": self.event_type,
            "documentId": self.document_id,
            "completedAt": self.completed_at,
        }
        if self.summary_url is not None:
            data["summaryUrl"] = self.summary_url
        if self.error is not None:
            data["error"] = self.error
        return data

    def to_json(self):
        return json.dumps(self.to_dict(), separators=(",", ":"), ensure_ascii=False)


def build_payload(document_id, event_type, summary_url=None, error=None):
    """Build a payload whose eventId is stable per document and event type."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unsupported webhook event type: {event_type}")
    completed_at = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    return WebhookPayload(
        event_id=str(uuid.uuid5(_EVENT_NAMESPACE, f"{document_id}:{event_type}")),
        event_type=event_type,
        document_id=document_id,
        completed_at=completed_at,
        summary_url=summary_url,
        error=error,
    )


def _to_bytes(value: Union[str, bytes]) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else value


def _compute_signature(payload: Union[str, bytes], secret: str, timestamp: int) -> str:
    message = f"{timestamp}.".encode("ascii") + _to_bytes(payload)
    return hmac.new(_to_bytes(secret), message, hashlib.sha256).hexdigest()


def sign_webhook_payload(payload, secret, timestamp=None):
    """Return a `t=<unix_seconds>,v1=<hex_signature>` header value."""
    if not secret:
        raise ValueError("Webhook secret is required")
    timestamp = int(time.time()) if timestamp is None else int(timestamp)
    return f"t={timestamp},v1={_compute_signature(payload, secret, timestamp)}"


def verify_webhook_signature(
    payload,
    signature_header,
    secret,
    tolerance_seconds=SIGNATURE_TOLERANCE_SECONDS,
    now=None,
):
    """Verify a signature against the exact raw request body."""
    if not secret or not signature_header:
        return False

    timestamp_text = None
    signatures = []
    for part in signature_header.split(","):
        key, separator, value = part.strip().partition("=")
        if not separator:
            return False
        if key == "t":
            timestamp_text = value
        elif key == "v1":
            signatures.append(value)

    if not timestamp_text or not signatures:
        return False
    if not (timestamp_text.isascii() and timestamp_text.isdigit()):
        return False

    timestamp = int(timestamp_text)
    current = int(time.time()) if now is None else int(now)
    if abs(current - timestamp) > tolerance_seconds:
        return False

    expected = _compute_signature(payload, secret, timestamp).encode("ascii")
    matched = False
    for candidate in signatures:
        candidate_bytes = candidate.encode("utf-8")
        if len(candidate_bytes) == len(expected) and hmac.compare_digest(
            candidate_bytes, expected
        ):
            matched = True
    return matched


def _private_urls_allowed():
    return os.environ.get("WEBHOOK_ALLOW_PRIVATE_URLS", "").lower() == "true"


def _is_blocked_ip(value):
    ip = ipaddress.ip_address(value.split("%", 1)[0])
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return not ip.is_global or ip.is_multicast


def validate_webhook_url(url, allow_private=None):
    """Reject URLs that could reach internal services. Performs no DNS lookup."""
    allow_private = _private_urls_allowed() if allow_private is None else allow_private
    if not isinstance(url, str) or not url or len(url) > MAX_WEBHOOK_URL_LENGTH:
        raise WebhookUrlError("Webhook URL is missing or too long")

    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        parts.port  # Raises ValueError for an invalid port.
    except ValueError as exc:
        raise WebhookUrlError("Webhook URL is malformed") from exc

    allowed_schemes = ("https", "http") if allow_private else ("https",)
    if parts.scheme not in allowed_schemes:
        raise WebhookUrlError("Webhook URL must use HTTPS")
    if not hostname:
        raise WebhookUrlError("Webhook URL must include a host")
    if parts.username is not None or parts.password is not None:
        raise WebhookUrlError("Webhook URL must not contain credentials")
    if any(ord(ch) <= 32 or ord(ch) == 127 for ch in url):
        raise WebhookUrlError("Webhook URL contains invalid characters")

    if not allow_private:
        lowered = hostname.lower().rstrip(".")
        if lowered == "localhost" or lowered.endswith(".localhost"):
            raise WebhookUrlError("Webhook URL must not target a private address")
        try:
            blocked = _is_blocked_ip(hostname)
        except ValueError:
            blocked = False  # A DNS name, checked again at connect time.
        if blocked:
            raise WebhookUrlError("Webhook URL must not target a private address")
    return url


def _connect_to_validated_address(address, timeout, allow_private):
    """Resolve and connect to a checked IP, which prevents DNS rebinding."""
    host, port = address
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips = list(dict.fromkeys(info[4][0] for info in infos))
    if not allow_private and any(_is_blocked_ip(ip) for ip in ips):
        raise WebhookUrlError("Webhook host resolves to a private address")

    last_error = None
    for ip in ips:
        try:
            return socket.create_connection((ip, port), timeout)
        except OSError as exc:
            last_error = exc
    raise last_error or OSError("Webhook host did not resolve")


def _post(url, body, headers, timeout_seconds):
    """POST once, enforce a whole-request deadline, and never follow redirects."""
    allow_private = _private_urls_allowed()
    parts = urlsplit(url)
    connection_class = (
        http.client.HTTPSConnection
        if parts.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_class(parts.hostname, parts.port, timeout=timeout_seconds)
    connection._create_connection = lambda address, timeout=None, source=None: (
        _connect_to_validated_address(address, timeout_seconds, allow_private)
    )
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"

    outcome = {}

    def send():
        try:
            connection.request("POST", target, body=body, headers=headers)
            response = connection.getresponse()
            outcome["status"] = response.status
            response.close()
        except BaseException as exc:  # Re-raised on the calling thread.
            outcome["error"] = exc

    worker = threading.Thread(target=send, daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    timed_out = worker.is_alive()
    connection.close()

    if timed_out:
        raise TimeoutError(f"Webhook request exceeded {timeout_seconds}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["status"]


def _is_retryable_status(status):
    return status in (408, 429) or 500 <= status <= 599


def dispatch_webhook(
    url,
    payload,
    secret,
    max_retries=4,
    base_delay_ms=1000,
    timeout_ms=5000,
    time_budget_ms=None,
):
    """Deliver a payload, retrying with full-jitter exponential backoff.

    `max_retries` excludes the first attempt. `time_budget_ms` stops retries
    before a caller's deadline. Returns a result dict or raises
    WebhookDeliveryError.
    """
    if not secret:
        raise ValueError("Webhook secret is required")
    try:
        validate_webhook_url(url)
    except WebhookUrlError as exc:
        raise WebhookDeliveryError(str(exc), attempts=0) from exc

    body_text = payload.to_json()
    body = body_text.encode("utf-8")
    started = time.monotonic()
    timeout_seconds = timeout_ms / 1000
    last_status = None
    last_reason = "no attempt made"
    attempts = 0

    def remaining_ms():
        if time_budget_ms is None:
            return None
        return time_budget_ms - (time.monotonic() - started) * 1000

    for attempt in range(max_retries + 1):
        remaining = remaining_ms()
        attempt_timeout = timeout_seconds
        if remaining is not None:
            if remaining <= 0:
                break
            attempt_timeout = min(timeout_seconds, remaining / 1000)

        headers = {
            "Content-Type": "application/json",
            # Signed per attempt so retries stay inside the replay window.
            "X-Webhook-Signature": sign_webhook_payload(body_text, secret),
            "X-Idempotency-Key": payload.event_id,
            "X-Webhook-Event": payload.event_type,
        }
        attempts = attempt + 1

        try:
            status = _post(url, body, headers, attempt_timeout)
        except WebhookUrlError as exc:
            raise WebhookDeliveryError(str(exc), attempts=attempts) from exc
        except (OSError, http.client.HTTPException) as exc:
            last_status = None
            last_reason = type(exc).__name__
        else:
            last_status = status
            last_reason = f"HTTP {status}"
            if 200 <= status < 300:
                return {"delivered": True, "status": status, "attempts": attempts}
            if not _is_retryable_status(status):
                raise WebhookDeliveryError(
                    f"Non-retryable response: HTTP {status}",
                    attempts=attempts,
                    status=status,
                )

        if attempt == max_retries:
            break

        delay_ms = int(random.random() * (base_delay_ms * (2**attempt)))
        remaining = remaining_ms()
        if remaining is not None and delay_ms >= remaining:
            break
        logger.warning(
            "Webhook attempt %d failed for event %s (%s); retrying in %d ms",
            attempts,
            payload.event_id,
            last_reason,
            delay_ms,
        )
        time.sleep(delay_ms / 1000)

    raise WebhookDeliveryError(
        f"Delivery failed after {attempts} attempt(s): {last_reason}",
        attempts=attempts,
        status=last_status,
    )


def _presign_summary_url(summary_path):
    """Return a temporary URL for an S3 summary, or None for local files."""
    if not summary_path or not summary_path.startswith("s3://"):
        return None
    import boto3

    bucket, _, key = summary_path[len("s3://") :].partition("/")
    return boto3.client("s3").generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=SUMMARY_URL_EXPIRY_SECONDS,
    )


def notify_job_outcome(
    job_id,
    event_type,
    webhook_url=None,
    summary_path=None,
    error=None,
    time_budget_ms=None,
):
    """Send a job webhook without ever raising into the processing pipeline."""
    try:
        url = webhook_url or os.environ.get("DEFAULT_WEBHOOK_URL")
        if not url:
            return False
        secret = os.environ.get("WEBHOOK_SECRET")
        if not secret:
            logger.error(
                "Webhook skipped for job %s: WEBHOOK_SECRET is not set", job_id
            )
            return False

        summary_url = None
        try:
            summary_url = _presign_summary_url(summary_path)
        except Exception as exc:
            logger.error(
                "Could not presign summary for job %s: %s", job_id, type(exc).__name__
            )

        payload = build_payload(
            job_id, event_type, summary_url=summary_url, error=error
        )
        result = dispatch_webhook(url, payload, secret, time_budget_ms=time_budget_ms)
        logger.warning(
            "Webhook delivered for job %s event %s (status=%s attempts=%s)",
            job_id,
            payload.event_id,
            result["status"],
            result["attempts"],
        )
        return True
    except WebhookDeliveryError as exc:
        logger.error(
            "Webhook delivery failed for job %s event %s (status=%s attempts=%s): %s",
            job_id,
            event_type,
            exc.status,
            exc.attempts,
            exc,
        )
    except Exception as exc:
        logger.error(
            "Unexpected webhook error for job %s: %s", job_id, type(exc).__name__
        )
    return False


def without_webhook_url(job):
    """Copy a job for API output, hiding a URL that may embed a token."""
    return {key: value for key, value in job.items() if key != "webhook_url"}
