"""
Structured JSON logging and context tracking for SIGS-RS.

SRS §3.3 & §4.3: Production logging architecture.
- Outputs structured JSON logs.
- Strictly redacts sensitive telemetry, PII, credentials, and raw sensor readings.
- Manages request and packet correlation IDs using contextvars.
"""
from __future__ import annotations

import contextvars
from datetime import datetime, timezone
import json
import logging
import sys
from typing import Any
import uuid

# ── Context Variables for Correlation Tracking ──────────────────────────────
_correlation_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)
_packet_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "packet_id", default=""
)


def set_correlation_id(cid: str | None = None) -> contextvars.Token:
    """Set correlation ID for current context. Generates a new one if not provided."""
    corr_id = cid or f"corr-{uuid.uuid4().hex[:12]}"
    return _correlation_id_ctx.set(corr_id)


def get_correlation_id() -> str:
    """Retrieve current correlation ID, or empty string if unset."""
    return _correlation_id_ctx.get()


def set_packet_id(pid: str | None = None) -> str:
    """Set packet ID for current context."""
    packet_id = pid or f"pkt-{uuid.uuid4().hex[:12]}"
    _packet_id_ctx.set(packet_id)
    return packet_id


def get_packet_id() -> str:
    """Retrieve current packet ID, or empty string if unset."""
    return _packet_id_ctx.get()


# ── Sensitive Keys / PII Redaction ──────────────────────────────────────────
EXACT_SENSITIVE_KEYS = frozenset({
    "latitude",
    "longitude",
    "lat",
    "lon",
    "mac_address",
    "mac",
    "geo",
    "auth",
    "raw_payload",
    "raw_bytes",
    "payload",
    "water_ext",
    "energy_ext",
    "air_quality_ext",
})

SUBSTRING_SENSITIVE_KEYS = (
    "latitude",
    "longitude",
    "mac_address",
    "password",
    "secret",
    "token",
    "authorization",
    "private_key",
    "client_key",
    "api_key",
    "device_id",
)

REDACTED_VALUE = "[REDACTED]"


def is_sensitive_key(key: str) -> bool:
    """Check if a dictionary key represents sensitive PII or credentials."""
    k_lower = str(key).lower()
    if k_lower in EXACT_SENSITIVE_KEYS:
        return True
    parts = set(k_lower.replace(".", "_").replace("-", "_").split("_"))
    if any(p in EXACT_SENSITIVE_KEYS for p in parts):
        return True
    return any(sub in k_lower for sub in SUBSTRING_SENSITIVE_KEYS)


import re

_SENSITIVE_KV_REGEX = re.compile(
    r'(?i)([\'"]?(?:latitude|longitude|lat|lon|mac_address|mac|password|secret|token|authorization|private_key|client_key|api_key|device_id|payload|raw_payload|raw_bytes|water_ext|energy_ext|air_quality_ext|input_value)[\'"]?\s*[:=]\s*)'
)
_MAC_REGEX = re.compile(r'\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b')
_BEARER_REGEX = re.compile(r'(?i)Bearer\s+[A-Za-z0-9\-_=~+/\.]+')
_JWT_REGEX = re.compile(r'\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b')

def _redact_sensitive_fallback(text: str) -> str:
    text = _BEARER_REGEX.sub('[REDACTED]', text)
    text = _JWT_REGEX.sub('[REDACTED]', text)
    text = _MAC_REGEX.sub('[REDACTED]', text)
    
    result = []
    idx = 0
    while idx < len(text):
        match = _SENSITIVE_KV_REGEX.search(text, idx)
        if not match:
            result.append(text[idx:])
            break
            
        result.append(text[idx:match.end()])
        
        val_start = match.end()
        val_end = val_start
        
        while val_end < len(text) and text[val_end].isspace():
            val_end += 1
            
        if val_end >= len(text):
            result.append("[REDACTED]")
            break
            
        char = text[val_end]
        if char in ('"', "'"):
            quote = char
            val_end += 1
            escaped = False
            while val_end < len(text):
                c = text[val_end]
                if escaped:
                    escaped = False
                elif c == '\\':
                    escaped = True
                elif c == quote:
                    val_end += 1
                    break
                val_end += 1
        elif char in ('{', '['):
            stack = []
            in_quote = None
            escaped = False
            while val_end < len(text):
                c = text[val_end]
                if in_quote:
                    if escaped:
                        escaped = False
                    elif c == '\\':
                        escaped = True
                    elif c == in_quote:
                        in_quote = None
                else:
                    if c in ('"', "'"):
                        in_quote = c
                    elif c in ('{', '['):
                        stack.append(c)
                    elif c == '}':
                        if stack and stack[-1] == '{':
                            stack.pop()
                        else:
                            break
                    elif c == ']':
                        if stack and stack[-1] == '[':
                            stack.pop()
                        else:
                            break
                val_end += 1
                if not stack:
                    break
        else:
            while val_end < len(text):
                c = text[val_end]
                if c in (',', '}', ']', '\n', '&'):
                    break
                val_end += 1
                
        result.append("[REDACTED]")
        idx = val_end

    return "".join(result)

def scrub_string(text: str) -> str:
    # 1. Attempt JSON parsing
    text_stripped = text.strip()
    if text_stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, (dict, list)):
                sanitized = redact_sensitive_data(parsed)
                # Ensure we don't accidentally re-introduce secrets by dumping a string containing secrets?
                # redact_sensitive_data recursively scrubs, so sanitized dict/list is safe.
                return json.dumps(sanitized)
        except json.JSONDecodeError:
            pass

    # 3. Fallback to safe non-JSON string scrubbing
    return _redact_sensitive_fallback(text)

def redact_sensitive_data(obj: Any) -> Any:
    """
    Recursively redact any known PII or sensitive secrets from dictionaries,
    lists, and primitive values before logging.
    """
    if isinstance(obj, dict):
        clean_dict = {}
        for k, v in obj.items():
            if is_sensitive_key(k):
                clean_dict[k] = REDACTED_VALUE
            else:
                clean_dict[k] = redact_sensitive_data(v)
        return clean_dict
    elif isinstance(obj, (list, tuple, set)):
        return [redact_sensitive_data(item) for item in obj]
    elif isinstance(obj, str):
        return scrub_string(obj)
    return obj


# ── JSON Log Formatter ───────────────────────────────────────────────────────
class JsonLogFormatter(logging.Formatter):
    """
    Standard library logging Formatter that outputs single-line JSON records.
    Automatically enriches records with correlation_id, packet_id, and redacts PII.
    """

    def format(self, record: logging.LogRecord) -> str:
        now_iso = datetime.now(timezone.utc).isoformat()

        # Base structured event
        log_entry: dict[str, Any] = {
            "timestamp": now_iso,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Correlation context
        corr_id = get_correlation_id()
        if corr_id:
            log_entry["correlation_id"] = corr_id

        pkt_id = get_packet_id()
        if pkt_id:
            log_entry["packet_id"] = pkt_id

        # Attach extra fields if present
        extra_keys = {
            k: v
            for k, v in record.__dict__.items()
            if k not in (
                "args",
                "asctime",
                "created",
                "exc_info",
                "exc_text",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "message",
                "msg",
                "name",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "stack_info",
                "thread",
                "threadName",
            )
        }
        if extra_keys:
            log_entry["extra"] = redact_sensitive_data(extra_keys)

        # Exception information
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        # Final PII scrubbing of entire entry
        safe_entry = redact_sensitive_data(log_entry)

        try:
            return json.dumps(safe_entry, default=str)
        except Exception:
            return json.dumps({
                "timestamp": now_iso,
                "level": record.levelname,
                "logger": record.name,
                "message": str(record.getMessage()),
            })


# ── Configuration & Logger retrieval ────────────────────────────────────────
def configure_logging(
    level: str = "INFO",
    json_format: bool = True,
) -> None:
    """
    Configure global application logging with structured JSON formatting.
    """
    root_logger = logging.getLogger()
    log_level = getattr(logging, level.upper(), logging.INFO)
    root_logger.setLevel(log_level)

    # Remove existing handlers to avoid duplicates
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    if json_format:
        stream_handler.setFormatter(JsonLogFormatter())
    else:
        stream_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )

    root_logger.addHandler(stream_handler)


def get_logger(name: str) -> logging.Logger:
    """Return a logger instance configured for the application."""
    return logging.getLogger(name)
