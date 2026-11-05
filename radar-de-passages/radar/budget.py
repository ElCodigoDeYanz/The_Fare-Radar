"""Monthly call budget per provider. Reservation is atomic and counts attempts, not successes."""

import time


def month_key(now):
    return time.strftime("%Y-%m", time.gmtime(now))


def reserve(db, provider, cap, now):
    """Count one call. Returns False (and counts nothing) when the cap is reached."""
    month = month_key(now)
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT OR IGNORE INTO provider_usage(provider,month,calls) VALUES (?,?,0)",
            (provider, month),
        )
        calls = con.execute(
            "SELECT calls FROM provider_usage WHERE provider=? AND month=?", (provider, month)
        ).fetchone()[0]
        if cap is not None and calls >= cap:
            return False
        con.execute(
            "UPDATE provider_usage SET calls=calls+1 WHERE provider=? AND month=?",
            (provider, month),
        )
        return True


def usage(db, now):
    month = month_key(now)
    with db.connect() as con:
        return {
            r["provider"]: r["calls"]
            for r in con.execute("SELECT provider,calls FROM provider_usage WHERE month=?", (month,))
        }
