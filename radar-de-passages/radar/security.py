"""Secrets and log hygiene. Practices taken from the reference crawler (politecrawl) that this
project lacked: a Secret wrapper that never prints its value, a logging filter that scrubs
credentials, and sanitising untrusted text (URLs, titles) before it reaches a log line."""

import logging
import re
from urllib.parse import urlsplit, urlunsplit

CREDENTIAL = re.compile(
    r"(?i)(?:authorization|cookie|set-cookie|token|api[_-]?key|password)\s*[:=]\s*(?:bearer\s+)?\S+"
    r"|bearer\s+\S+"
    r"|bot\d{6,}:[A-Za-z0-9_-]{20,}"  # Telegram bot token inside a URL
    r"|://[^/\s:@]+:[^/\s@]+@"  # user:password@ inside any URL
)
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class Secret:
    """Wraps a credential so it never appears in logs, reprs or JSON."""

    __slots__ = ("_value",)

    def __init__(self, value):
        self._value = value

    def reveal(self):
        return self._value

    def __repr__(self):
        return "Secret(***)"

    __str__ = __repr__

    def __bool__(self):
        return bool(self._value)


def sanitize_for_log(value, max_len=300):
    """Neutralise untrusted text before logging: no newlines (log injection), no control chars."""
    value = str(value).replace("\r", "\\r").replace("\n", "\\n")
    value = CONTROL.sub("?", value)
    return value if len(value) <= max_len else value[:max_len] + "..."


def redact(text, secrets=()):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return CREDENTIAL.sub("***REDACTED***", text)


def strip_credentials(url):
    """URL without user:password, for logs and the incident journal."""
    parts = urlsplit(url)
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    return url


class RedactingFilter(logging.Filter):
    def __init__(self, secrets=()):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record):
        record.msg = redact(record.getMessage(), self.secrets)
        record.args = ()
        return True


def install_log_redaction(secrets=()):
    """Attach the scrubbing filter to every handler of the root logger (idempotent) and keep the
    HTTP client libraries from logging every request URL at INFO (Telegram's token is part of its URL)."""
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter(secrets))
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return root
