"""Structured, secret-redacting logging.

Ported from person-matching-service's observability/logging.py, minus its
OpenTelemetry trace-correlation piece. OTel instrumentation here comes from
the OTel Operator's auto-instrumentation (otel.autoInstrumentation.enabled
in dev-ue1/staging-ue1 Helm values, now in icanbwell/bwell-cms-hte-patient-matching-service, BAI-622), which
patches the process from outside application code -- it does not stamp
trace_id/span_id onto log records. Port TraceContextFilter from the source
above if log-trace correlation in Groundcover is needed.

Two composable pieces:

* RedactSecretsFilter -- renders the record's message and redacts secrets
  into record._redacted, without mutating the original msg.
* RedactingJsonFormatter -- emits JSON, prefers the pre-redacted message
  when present, and scrubs secrets from every other string field.

This module handles secrets (tokens, passwords, auth headers). It does not
scrub PHI (patient names, DOBs, SSNs) from log bodies -- callers must not log
raw FHIR Patient payloads. See patient_matching_service/service/match_controller.py
for where that boundary is enforced.
"""

import logging
import os
import re
import sys
from typing import Any

from pythonjsonlogger.json import JsonFormatter

_URL_CREDENTIALS = re.compile(r"://[^:/?#@\s]+:[^@/?#\s]+@")

_SECRET_KEY_VALUE = re.compile(
    r"(?<![\w-])"
    r"((?:[\w-]*[_-])?"
    r"(?:token|password|passwd|pwd|secret|key|auth|authorization)"
    r"(?:[_-][\w-]*)?"
    r"[\"']?\s*[=:]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)",
    re.IGNORECASE,
)

_BEARER_TOKEN = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE)


def redact(text: str) -> str:
    """Return text with known secret shapes replaced by ***."""
    text = _URL_CREDENTIALS.sub("://***:***@", text)
    text = _SECRET_KEY_VALUE.sub(r"\1***", text)
    text = _BEARER_TOKEN.sub(r"\1***", text)
    return text


class RedactSecretsFilter(logging.Filter):
    """Render and redact the message into record._redacted['message'].

    The original record.msg and record.args are left untouched so other
    handlers/formatters are unaffected and lazy % args still work.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record._redacted = {"message": redact(record.getMessage())}
        return True


class RedactingJsonFormatter(JsonFormatter):
    """JSON formatter that prefers pre-redacted messages and scrubs fields."""

    def add_fields(
        self,
        log_data: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_data, record, message_dict)
        redacted = getattr(record, "_redacted", None)
        if redacted and "message" in redacted:
            log_data["message"] = redacted["message"]

    def process_log_record(self, log_data: dict[str, Any]) -> dict[str, Any]:
        for key, value in log_data.items():
            if isinstance(value, str):
                log_data[key] = redact(value)
        return super().process_log_record(log_data)


def configure_logging(level: str | None = None) -> None:
    """Install JSON, secret-redacting logging on the root logger.

    Idempotent: each call replaces the root handlers with one freshly
    configured handler, so repeated calls (e.g. once per uvicorn worker) do
    not stack duplicate handlers.

    Level resolves from the explicit argument, else the LOG_LEVEL env var,
    else INFO.
    """
    log_level = (level or os.getenv("LOG_LEVEL") or "INFO").upper()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        RedactingJsonFormatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    )
    handler.addFilter(RedactSecretsFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(log_level)
