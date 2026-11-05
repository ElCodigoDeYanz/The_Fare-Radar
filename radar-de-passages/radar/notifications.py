"""Telegram transport and durable send state. Real delivery requires explicit opt-in."""

import os
import time
import httpx


def _telegram_config():
    if os.environ.get("RADAR_TELEGRAM_ENABLED") != "1":
        raise RuntimeError(
            "Telegram real desactivado. Actívalo explícitamente para tu propio chat."
        )
    token = os.environ.get("RADAR_TELEGRAM_TOKEN")
    chat_id = os.environ.get("RADAR_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError(
            "Falta token o chat_id. Nunca los publiques ni los incluyas en capturas."
        )
    return token, chat_id


def notify_incidents(db, client=None, now=None):
    """Tell the owner about important access problems (explicit blocks, open circuits).
    One message per domain per hour at most; an ambiguous send is never repeated.
    notified: 0 pending, 1 sent or ambiguous, 2 suppressed (a message for that domain went out
    within the last hour) or refused by Telegram."""
    now = time.time() if now is None else now
    token, chat_id = _telegram_config()
    own_client = client is None
    client = client or httpx.Client(timeout=8, follow_redirects=False, trust_env=False)
    sent = []
    try:
        with db.connect() as con:
            pending = [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM incidents WHERE notified=0 AND severity='high' ORDER BY at LIMIT 50"
                )
            ]
        by_domain = {}
        for row in pending:
            by_domain.setdefault(row["domain"], []).append(row)
        for domain, rows in by_domain.items():
            latest = rows[-1]
            with db.connect() as con:
                recent = con.execute(
                    "SELECT COUNT(*) FROM incidents WHERE domain=? AND notified=1 AND at>?", (domain, now - 3600)
                ).fetchone()[0]
            state = 2 if recent else 1
            if not recent:
                text = (
                    f"[RADAR · INCIDENTE] {domain}: {latest['kind']}"
                    f"{' (HTTP ' + str(latest['http_status']) + ')' if latest['http_status'] else ''}. "
                    f"{latest['detail']} Acción: {latest['action']}. "
                    f"Revisa la fuente y reanúdala con: python -m radar.main --resume {domain}"
                )
                try:
                    response = client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": text},
                    )
                    ok = response.status_code == 200 and response.json().get("ok") is True
                    state = 1 if ok or response.status_code >= 500 else 2
                except (httpx.HTTPError, ValueError):
                    state = 1  # ambiguous: do not repeat
            with db.connect() as con:
                con.executemany("UPDATE incidents SET notified=? WHERE id=?", [(state, r["id"]) for r in rows])
            sent.append({"domain": domain, "notified": state})
    finally:
        if own_client:
            client.close()
    return sent


def deliver_telegram(db, client=None):
    token, chat_id = _telegram_config()
    own_client = client is None
    client = client or httpx.Client(timeout=8, follow_redirects=False, trust_env=False)
    result = []
    try:
        # No automatic retry after an ambiguous send: Telegram sendMessage has no idempotency key.
        with db.connect() as con:
            con.execute(
                "UPDATE alerts SET status='uncertain',error='Proceso interrumpido durante el envío. Revisar el chat antes de reenviar.' WHERE status='sending'"
            )
            ids = [
                r[0]
                for r in con.execute(
                    "SELECT id FROM alerts WHERE channel='telegram' AND status='pending' ORDER BY priority_rank DESC, created_at LIMIT 100"
                )
            ]
        for key in ids:
            with db.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute(
                    "SELECT * FROM alerts WHERE id=? AND status='pending'", (key,)
                ).fetchone()
                if not row:
                    continue
                obs = con.execute(
                    "SELECT expires_at FROM observations WHERE id=?",
                    (row["observation_id"],),
                ).fetchone()
                if not obs or obs[0] <= time.time():
                    con.execute(
                        "UPDATE alerts SET status='failed',error='La observación caducó antes del envío.' WHERE id=?",
                        (key,),
                    )
                    continue
                con.execute(
                    "UPDATE alerts SET status='sending',attempts=attempts+1 WHERE id=?",
                    (key,),
                )
            try:
                response = client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": row["message"]},
                )
                payload = response.json()
                if (
                    response.status_code == 200
                    and isinstance(payload, dict)
                    and payload.get("ok") is True
                ):
                    status, error = "sent", None
                elif response.status_code >= 500:
                    status, error = (
                        "uncertain",
                        "El servicio no confirmó la entrega. Revisar el chat.",
                    )
                else:
                    status, error = (
                        "failed",
                        f"Envío rechazado (HTTP {response.status_code}); no se guarda el cuerpo de la respuesta.",
                    )
            except (httpx.HTTPError, ValueError):
                status, error = (
                    "uncertain",
                    "Entrega sin confirmar; no se reintenta automáticamente para evitar duplicados.",
                )
            with db.connect() as con:
                con.execute(
                    "UPDATE alerts SET status=?,error=? WHERE id=?",
                    (status, error, key),
                )
            result.append({"id": key, "status": status})
    finally:
        if own_client:
            client.close()
    return result
