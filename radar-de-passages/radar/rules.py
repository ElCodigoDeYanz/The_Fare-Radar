"""Alert rules. No fake discounts, no predictions, no Telegram calls.

alert_mode 'target': absolute price target (original behaviour).
alert_mode 'smart' : priority tiers from radar.priority (history and other stores).
alert_mode 'both'  : either one raises the alert; the higher priority wins.
Every mode requires a live, same-currency offer and (products) confirmed stock.
"""

from .priority import RANK

COOLDOWN_S = 1800
ESCALATION_WINDOW_S = 86400


def decide(watch, metrics, now, priority=None, last_alert=None):
    """Returns (alert?, reason, tier). last_alert: (created_at, rank) of the latest alert."""
    mode = watch.get("alert_mode") or "target"
    if not watch["enabled"]:
        return False, "Alertas desactivadas.", None
    if metrics["expires_at"] <= now:
        return False, "La oferta observada está caducada.", None
    if metrics["currency"] != watch["criteria"]["currency"]:
        return False, "Moneda distinta del seguimiento.", None
    if metrics.get("availability", "n/a") not in ("n/a", "in_stock"):
        return False, "Producto sin disponibilidad confirmada en la tienda.", None
    by_target = mode in ("target", "both") and metrics["current_total_cents"] <= watch["target_cents"]
    tier = priority["tier"] if mode in ("smart", "both") and priority else None
    if not by_target and not tier:
        if mode == "smart":
            return False, "Sin variación extraordinaria frente a historial u otras tiendas.", None
        return False, "Precio total superior al objetivo.", None
    options = []
    if tier:
        options.append((tier, RANK[tier]))
    if by_target:
        options.append(("objetivo", RANK["objetivo"]))
    label, rank = max(options, key=lambda o: o[1])  # ties keep the more informative tier
    if last_alert is not None:
        age, last_rank = now - last_alert[0], last_alert[1]
        in_cooldown = age < COOLDOWN_S
        escalates = rank > last_rank and age < ESCALATION_WINDOW_S
        if in_cooldown and not escalates:
            return False, "Cooldown de 30 minutos activo.", None
    reason = (
        "Precio total observado menor o igual al objetivo configurado; verificar con el proveedor."
        if label == "objetivo"
        else f"Prioridad {label.upper()}: " + " ".join(priority["reasons"])
    )
    return True, reason, label
