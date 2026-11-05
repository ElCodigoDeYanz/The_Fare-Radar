"""Central detector of access restrictions. One place decides what a response means.

It classifies; it never tries to get around anything. The scheduler's reaction is fixed:
  PAUSE_KINDS    explicit rejection (403/429/captcha/denied/session/redirect) -> the WHOLE domain
                 is paused with exponential backoff, the circuit opens after N in a row, and a
                 human must resume it. No retry on another network exit, no new identity.
  WATCH_KINDS    this page is the problem (structure changed, gone, empty): that watch backs off
                 and is paused after repeated failures; the domain keeps working.
  TRANSIENT      timeouts, network and 5xx: exponential backoff, limited attempts.
"""

import re
from urllib.parse import urlsplit
import uuid
from .providers.base import (
    AccessDeniedError,
    BlockedError,
    CaptchaError,
    EmptyResponseError,
    GoneError,
    NetworkFailure,
    RateLimitedError,
    SessionError,
    TimeoutFailure,
    TransientError,
    UnexpectedRedirectError,
)

PAUSE_KINDS = frozenset({"blocked", "rate_limited", "captcha", "denied", "session", "redirect"})
WATCH_KINDS = frozenset({"structure", "gone", "empty"})
TRANSIENT_KINDS = frozenset({"transient", "timeout", "network"})
HIGH = PAUSE_KINDS | {"circuit_open"}

CAPTCHA_MARKERS = (
    "captcha", "recaptcha", "hcaptcha", "px-captcha", "cf-chl", "challenge-platform",
    "just a moment...", "are you a robot", "verify you are human", "unusual traffic",
    "verificación de seguridad", "verifica que eres humano",
)
DENIED_MARKERS = (
    "access denied", "request blocked", "you have been blocked", "acceso denegado",
    "forbidden", "datadome", "incident id", "reference #", "has sido bloqueado",
)
SUSPECT_PATH = re.compile(r"(?i)/(captcha|challenge|blocked|denied|login|signin|sign-in|account/login|verify|sorry)(/|\?|$|\.)")
MIN_BODY = 400  # a real HTML page is bigger; less means empty/truncated


def classify_redirect(from_url, to_url, allowed):
    """None when the redirect is ordinary, else an UnexpectedRedirectError/SessionError."""
    src, dst = urlsplit(from_url), urlsplit(to_url)
    if not allowed(dst.hostname or ""):
        return UnexpectedRedirectError("La tienda redirige fuera de los dominios permitidos.")
    if SUSPECT_PATH.search(dst.path + ("?" + dst.query if dst.query else "")) and not SUSPECT_PATH.search(src.path):
        if re.search(r"(?i)login|signin|sign-in", dst.path):
            return SessionError("La tienda redirige a una página de inicio de sesión.")
        return UnexpectedRedirectError("La tienda redirige a una página de verificación o bloqueo.")
    return None


def classify_block(status, body, headers=None, retry_after=None, has_data=False):
    """Only the explicit refusals: 429, 401, a human-verification page, 'access denied'. Nothing
    about missing pages, server errors or empty bodies. has_data: the caller already extracted a
    price from a 200 page, so words like 'captcha' in its scripts mean nothing (a recaptcha
    newsletter form is not a block)."""
    low = body[:20000].lower() if isinstance(body, str) else ""
    if status == 200 and has_data:
        return None
    if status == 429:
        return RateLimitedError("La tienda respondió 429 (demasiadas solicitudes).", retry_after)
    if status == 401:
        return SessionError("La tienda pide autenticación (HTTP 401).")
    if status in (200, 403, 451, 503) and any(m in low for m in CAPTCHA_MARKERS):
        return CaptchaError(
            "La tienda mostró una verificación humana (CAPTCHA); no se intenta resolver.", retry_after
        )
    if status in (403, 451):
        return AccessDeniedError(f"La tienda rechazó la consulta (HTTP {status}).", retry_after)
    if status == 503 and any(m in low for m in DENIED_MARKERS):
        return AccessDeniedError(f"La tienda rechazó la consulta (HTTP {status}).", retry_after)
    if status == 200 and any(m in low[:4000] for m in ("access denied", "request blocked", "acceso denegado")):
        return AccessDeniedError("La tienda mostró una página de acceso denegado.", retry_after)
    return None


def classify_response(status, body, headers=None, retry_after=None, has_data=False):
    """Map an HTTP answer to a typed error, or None when it looks like a normal page."""
    blocked = classify_block(status, body, headers, retry_after, has_data)
    if blocked is not None:
        return blocked
    if status == 200 and has_data:
        return None
    if status in (404, 410):
        return GoneError("La página del producto ya no existe (404/410).")
    if status >= 500:
        return TransientError(f"La tienda respondió HTTP {status}.")
    if status == 200 and len(body.strip()) < MIN_BODY:
        return EmptyResponseError("Respuesta vacía o incompleta (menos de 400 bytes).")
    return None


def classify_exception(exc):
    """httpx transport errors -> typed transient failures (limited, backed-off retries)."""
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return TimeoutFailure(f"Tiempo de espera agotado ({type(exc).__name__}).")
    return NetworkFailure(f"Error de red al consultar la fuente ({type(exc).__name__}).")


def classify_exception_text(kind):
    """For transports that do not use httpx (the optional browser renderer)."""
    if kind == "timeout":
        return TimeoutFailure("Tiempo de espera agotado al renderizar la página.")
    return NetworkFailure("Error de red al renderizar la página.")


def severity(kind):
    if kind in HIGH:
        return "high"
    if kind in WATCH_KINDS:
        return "medium"
    return "low"


def action_for(kind):
    if kind in PAUSE_KINDS:
        return "domain_paused"
    if kind in WATCH_KINDS:
        return "watch_backoff"
    return "retry_backoff"


def record_incident(db, now, *, domain, kind, detail, provider=None, watch_id=None,
                    catalog_id=None, url=None, status=None, egress=None, action=None):
    """Append-only journal of every restriction: who, when, which request, which answer."""
    from .security import redact, strip_credentials

    with db.connect() as con:
        con.execute(
            "INSERT INTO incidents(id,at,domain,provider,watch_id,catalog_id,url,http_status,kind,severity,action,egress,detail) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), now, domain, provider, watch_id, catalog_id,
                strip_credentials(url)[:500] if url else None, status, kind, severity(kind),
                action or action_for(kind), egress, redact(str(detail))[:500],
            ),
        )


def incidents(db, limit=100, domain=None):
    with db.connect() as con:
        if domain:
            rows = con.execute("SELECT * FROM incidents WHERE domain=? ORDER BY at DESC LIMIT ?", (domain, limit))
        else:
            rows = con.execute("SELECT * FROM incidents ORDER BY at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]
