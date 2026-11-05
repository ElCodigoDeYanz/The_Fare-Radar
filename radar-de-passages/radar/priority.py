"""Alert priority. Pure functions: how extraordinary is this price?

A price is judged against (1) the same product's own history, weighted by how long each
price was seen (confirmations), and (2) the same product's price in OTHER stores (matched by
GTIN/MPN, see radar.identity). Nothing here predicts the future or uses AI: it is a
transparent percentage-below-reference rule whose thresholds you can read and change.

Tiers by percentage below the reference price:
    baja >= 8 %   media >= 15 %   alta >= 25 %   maxima >= 40 %
'maxima' additionally needs the price to be at or below the lowest price ever seen, and a
history long enough to mean something. A price >= 70 % below reference is flagged as a
probable pricing error (stores often cancel those orders).
The "margen" is gross potential (reference / price - 1): it ignores shipping, fees and taxes.
"""

from dataclasses import dataclass
import os

TIERS = ("baja", "media", "alta", "maxima")
RANK = {None: 0, "objetivo": 1, "baja": 1, "media": 2, "alta": 3, "maxima": 4}
LABEL = {"objetivo": "OBJETIVO", "baja": "BAJA", "media": "MEDIA", "alta": "ALTA", "maxima": "MÁXIMA"}


@dataclass(frozen=True)
class Thresholds:
    baja: float = 8.0
    media: float = 15.0
    alta: float = 25.0
    maxima: float = 40.0
    suspect: float = 70.0
    min_sightings: int = 6  # confirmations of earlier prices needed to trust the history
    min_span_s: float = 3600.0  # ... spread over at least this long
    peer_gap: float = 5.0  # another store this % cheaper downgrades the alert one level

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        base = cls()

        def num(name, default, cast=float):
            try:
                return cast(env.get(name, default))
            except (TypeError, ValueError):
                return default

        values = {
            "baja": num("RADAR_TIER_BAJA_PCT", base.baja),
            "media": num("RADAR_TIER_MEDIA_PCT", base.media),
            "alta": num("RADAR_TIER_ALTA_PCT", base.alta),
            "maxima": num("RADAR_TIER_MAXIMA_PCT", base.maxima),
            "suspect": num("RADAR_TIER_SUSPECT_PCT", base.suspect),
            "min_sightings": num("RADAR_TIER_MIN_SIGHTINGS", base.min_sightings, int),
            "min_span_s": num("RADAR_TIER_MIN_HISTORY_S", base.min_span_s),
            "peer_gap": num("RADAR_TIER_PEER_GAP_PCT", base.peer_gap),
        }
        ladder = [values["baja"], values["media"], values["alta"], values["maxima"]]
        if not all(0 < v < 100 for v in ladder) or ladder != sorted(ladder):
            return base  # a nonsensical ladder falls back to the documented one
        return cls(**values)


DEFAULT = Thresholds()


def tier_for(drop_percent, th):
    tier = None
    for name in TIERS:
        if drop_percent >= getattr(th, name):
            tier = name
    return tier


def _down(tier):
    index = TIERS.index(tier)
    return TIERS[index - 1] if index else None


def weighted_median(pairs):
    """pairs: (value, weight>0). The value where half of the total weight lies on each side."""
    ordered = sorted((v, w) for v, w in pairs if w > 0)
    if not ordered:
        return None
    half = sum(w for _, w in ordered) / 2
    running = 0
    for value, weight in ordered:
        running += weight
        if running >= half:
            return value
    return ordered[-1][0]


def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def classify(stats, peer_prices=(), th=DEFAULT):
    """stats: analytics.summarize() output. peer_prices: current in-stock prices (cents, same
    currency, same condition) of the same product in OTHER stores.
    Returns {tier, rank, ...}; tier is None when nothing extraordinary (or not enough evidence)."""
    out = {
        "tier": None,
        "rank": 0,
        "drop_percent": None,
        "margin_percent": None,
        "reference_cents": None,
        "reference": None,
        "suspect_error": False,
        "peer_count": len(peer_prices),
        "best_peer_cents": min(peer_prices) if peer_prices else None,
        "reasons": [],
    }
    if stats.get("availability", "n/a") not in ("n/a", "in_stock"):
        out["reasons"].append("Sin disponibilidad confirmada: no se asigna prioridad.")
        return out
    price = stats["current_total_cents"]
    candidates = []  # (tier, drop, reference_cents, label)

    sightings = stats.get("history_sightings") or 0
    span = stats.get("history_span_s") or 0
    median = stats.get("median_cents")
    if median and stats.get("comparable_count"):
        if sightings >= th.min_sightings and span >= th.min_span_s:
            drop = (median - price) / median * 100
            tier = tier_for(drop, th)
            if tier == "maxima" and price > (stats.get("minimum_cents") or price):
                tier = "alta"  # not a new low: an extraordinary drop must be the lowest ever
            candidates.append((tier, drop, median, "mediana histórica"))
        else:
            out["reasons"].append(
                f"Historial insuficiente ({sightings} confirmaciones en {span / 3600:.1f} h; "
                f"se piden {th.min_sightings} en {th.min_span_s / 3600:.1f} h)."
            )

    if len(peer_prices) >= 2:
        peer_median = _median(peer_prices)
        drop = (peer_median - price) / peer_median * 100
        tier = tier_for(drop, th)
        if tier == "maxima":
            tier = "alta"  # cross-store alone never reaches the top tier
        candidates.append((tier, drop, peer_median, "mediana de otras tiendas"))

    scored = [c for c in candidates if c[0]]
    if not scored:
        if candidates:
            best = max(candidates, key=lambda c: c[1])
            out.update(drop_percent=round(best[1], 2), reference_cents=round(best[2]), reference=best[3])
            out["reasons"].append(f"Variación frente a la {best[3]}: {best[1]:.1f} %, bajo el umbral.")
        return out

    tier, drop, reference, label = max(scored, key=lambda c: (RANK[c[0]], c[1]))
    if out["best_peer_cents"] is not None and price > out["best_peer_cents"] * (1 + th.peer_gap / 100):
        tier = _down(tier)
        out["reasons"].append("Otra tienda lo vende más barato: se baja un nivel.")
    out.update(
        drop_percent=round(drop, 2),
        margin_percent=round((reference - price) / price * 100, 2),
        reference_cents=round(reference),
        reference=label,
        suspect_error=drop >= th.suspect,
    )
    out["tier"], out["rank"] = tier, RANK[tier]
    if tier:
        out["reasons"].append(f"{drop:.1f} % bajo la {label}.")
    return out
