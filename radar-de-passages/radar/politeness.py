"""Per-domain politeness, shared by the scheduler and manual refreshes.

What reduces bans in practice is not randomness alone but: never hitting one site in bursts
(minimum jittered gap between ANY two requests to the same domain), slowing down when the
site pushes back (exponential backoff on 403/429/challenge, honouring Retry-After) and
stopping completely after repeated blocks (circuit breaker, needs a human to resume).
It does not hide the monitor: no rotating identities, no header spoofing, no CAPTCHA solving.

AutoThrottle (the same algorithm as Scrapy's, https://docs.scrapy.org/en/latest/topics/autothrottle.html):
after every response, target_delay = latency / target_concurrency and the new per-domain delay is
the average of the previous delay and that target, clamped to max_delay; a non-200 answer can raise
the delay but never lower it. Here it works as a FLOOR on top of the jittered gap: it only matters
when a site is slow (latency above the gap), which is exactly when it needs more room.
"""

import random
import time


class DomainGate:
    def __init__(
        self,
        db,
        *,
        gap=(15.0, 45.0),
        backoff_base=900.0,
        backoff_cap=21600.0,
        disable_after=5,
        rng=None,
        target_concurrency=1.0,
        max_delay=60.0,
        trouble_after=3,
    ):
        self.db = db
        self.gap = gap
        self.backoff_base, self.backoff_cap = backoff_base, backoff_cap
        self.disable_after = disable_after
        self.rng = rng or random.Random()
        self.target_concurrency, self.max_delay = float(target_concurrency), float(max_delay)
        self.trouble_after = trouble_after

    def reserve(self, domain, now, hint=0.0):
        """Atomically claim the right to send ONE request now. False when not allowed."""
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT OR IGNORE INTO domain_state(domain) VALUES (?)", (domain,))
            row = con.execute("SELECT * FROM domain_state WHERE domain=?", (domain,)).fetchone()
            if row["status"] == "disabled" or row["blocked_until"] > now or row["next_allowed_at"] > now:
                return False
            wait = max(self.rng.uniform(*self.gap), float(hint or 0), float(row["delay_s"] or 0))
            con.execute(
                "UPDATE domain_state SET next_allowed_at=?,updated_at=? WHERE domain=?",
                (now + wait, now, domain),
            )
            return True

    def check(self, domain, now):
        """Read-only view: (allowed, reason, retry_in_seconds)."""
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM domain_state WHERE domain=?", (domain,)).fetchone()
        if row is None:
            return True, "", 0.0
        if row["status"] == "disabled":
            return False, "Dominio desactivado tras bloqueos repetidos; reanúdalo a mano.", None
        wait = max(row["blocked_until"], row["next_allowed_at"]) - now
        if wait > 0:
            reason = "Dominio en pausa por bloqueo." if row["blocked_until"] > now else "Espaciado mínimo entre consultas al dominio."
            return False, reason, wait
        return True, "", 0.0

    def record_success(self, domain, now):
        with self.db.connect() as con:
            con.execute(
                "UPDATE domain_state SET block_count=0,fail_streak=0,status='ok',blocked_until=0,last_error=NULL,updated_at=? WHERE domain=?",
                (now, domain),
            )

    def record_request(self, domain, now, latency_s, status, nbytes, egress=None):
        """Traffic accounting plus the AutoThrottle update (see module docstring)."""
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT OR IGNORE INTO domain_state(domain) VALUES (?)", (domain,))
            row = con.execute("SELECT delay_s FROM domain_state WHERE domain=?", (domain,)).fetchone()
            previous = float(row["delay_s"] or 0)
            delay = previous
            if latency_s is not None:
                target = latency_s / self.target_concurrency
                delay = (previous + target) / 2
                if status != 200:
                    delay = max(delay, previous)  # errors may slow us down, never speed us up
                delay = min(delay, self.max_delay)
            con.execute(
                "UPDATE domain_state SET requests=requests+1,bytes_in=bytes_in+?,latency_s=COALESCE(?,latency_s),"
                "delay_s=?,egress=COALESCE(?,egress),updated_at=? WHERE domain=?",
                (int(nbytes or 0), latency_s, delay, egress, now, domain),
            )
            return delay

    def record_trouble(self, domain, now):
        """Transient failures (timeouts, 5xx, network) are not blocks, but a domain that keeps
        failing must not be hammered: after `trouble_after` in a row it cools off, doubling each time.
        Returns the resume time, or None while below the threshold."""
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT OR IGNORE INTO domain_state(domain) VALUES (?)", (domain,))
            streak = con.execute("SELECT fail_streak FROM domain_state WHERE domain=?", (domain,)).fetchone()[0] + 1
            until = None
            if streak >= self.trouble_after:
                until = now + min(3600.0, 120.0 * 2 ** (streak - self.trouble_after))
                con.execute(
                    "UPDATE domain_state SET fail_streak=?,blocked_until=MAX(blocked_until,?),status=CASE WHEN status='disabled' THEN status ELSE 'cooling' END,updated_at=? WHERE domain=?",
                    (streak, until, now, domain),
                )
            else:
                con.execute("UPDATE domain_state SET fail_streak=?,updated_at=? WHERE domain=?", (streak, now, domain))
            return until

    def record_block(self, domain, now, retry_after=None, error=""):
        """Pause the domain. Returns the timestamp when requests may resume (or None if disabled)."""
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT OR IGNORE INTO domain_state(domain) VALUES (?)", (domain,))
            count = con.execute("SELECT block_count FROM domain_state WHERE domain=?", (domain,)).fetchone()[0] + 1
            backoff = min(self.backoff_cap, self.backoff_base * 2 ** (count - 1))
            wait = max(float(retry_after or 0), backoff * self.rng.uniform(1.0, 1.25))
            wait = min(wait, max(self.backoff_cap, float(retry_after or 0)))
            disabled = count >= self.disable_after
            con.execute(
                "UPDATE domain_state SET block_count=?,status=?,blocked_until=?,last_error=?,updated_at=? WHERE domain=?",
                (count, "disabled" if disabled else "cooling", now + wait, error[:300], now, domain),
            )
            return None if disabled else now + wait

    def resume(self, domain, now=None):
        now = time.time() if now is None else now
        with self.db.connect() as con:
            cur = con.execute(
                "UPDATE domain_state SET status='ok',block_count=0,fail_streak=0,blocked_until=0,next_allowed_at=0,last_error=NULL,updated_at=? WHERE domain=?",
                (now, domain),
            )
            return cur.rowcount > 0

    def states(self):
        with self.db.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM domain_state ORDER BY domain")]
