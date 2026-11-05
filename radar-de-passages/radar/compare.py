"""Same product, different stores. Groups watches that share a match_key (a valid GTIN, or brand +
part number: see radar.identity) and lines up each store's latest price. Only like with like:
same currency and same stated condition (new/used), and one row per store (domain).
Prices are NOT converted between currencies and out-of-stock rows are listed but never ranked."""

import json
import time
from urllib.parse import urlsplit


def compare_groups(db, now=None, limit=100):
    now = time.time() if now is None else now
    groups = {}
    with db.connect() as con:
        watches = con.execute(
            "SELECT id,match_key,title,label,category,subcategory,criteria_json FROM watches "
            "WHERE match_key IS NOT NULL AND enabled=1 AND kind='product'"
        ).fetchall()
        for w in watches:
            obs = con.execute(
                "SELECT total_cents,currency,conditions_json,observed_at,expires_at FROM observations "
                "WHERE watch_id=? ORDER BY observed_at DESC,id DESC LIMIT 1",
                (w["id"],),
            ).fetchone()
            if obs is None:
                continue
            cond = json.loads(obs["conditions_json"])
            url = json.loads(w["criteria_json"]).get("url", "")
            key = (w["match_key"], obs["currency"], cond.get("condition"))
            group = groups.setdefault(
                key,
                {"match_key": w["match_key"], "currency": obs["currency"], "condition": cond.get("condition"),
                 "title": w["title"] or w["label"], "category": w["category"], "subcategory": w["subcategory"], "stores": {}},
            )
            host = urlsplit(url).hostname
            row = {
                "watch_id": w["id"], "domain": host, "url": url, "price_cents": obs["total_cents"],
                "availability": cond.get("availability"), "seller": cond.get("seller"),
                "observed_at": obs["observed_at"], "expired": obs["expires_at"] <= now,
            }
            current = group["stores"].get(host)
            if current is None or row["observed_at"] > current["observed_at"]:
                group["stores"][host] = row
            group["title"] = group["title"] or w["title"]
    out = []
    for group in groups.values():
        stores = sorted(group.pop("stores").values(), key=lambda r: r["price_cents"])
        if len(stores) < 2:
            continue
        ranked = [r for r in stores if r["availability"] == "in_stock" and not r["expired"]]
        group["stores"] = stores
        group["ranked_count"] = len(ranked)
        if len(ranked) >= 2:
            low, high = ranked[0]["price_cents"], ranked[-1]["price_cents"]
            group["cheapest"] = ranked[0]["domain"]
            group["spread_percent"] = round((high - low) / low * 100, 2)
            group["saving_cents"] = high - low
        else:
            group["cheapest"], group["spread_percent"], group["saving_cents"] = None, None, None
        out.append(group)
    out.sort(key=lambda g: (g["spread_percent"] is None, -(g["spread_percent"] or 0)))
    return out[:limit]
