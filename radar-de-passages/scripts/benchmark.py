"""What does the monitor itself cost? Local benchmark with a simulated clock and a mock network.

Nothing leaves this machine: the "stores" are an in-process stub that answers robots.txt and a product
page of --page-kb with a schema.org JSON-LD price. The scheduler, the politeness gate (15-45 s between
requests to one site), AutoThrottle, the parser, the database and the alert rules are the real ones.

    python scripts/benchmark.py --watches 500 --domains 25 --hours 6

What it measures: CPU time, peak memory, database growth, downloaded body bytes, requests, and whether the
politeness invariants held under load (minimum gap per site, never two requests in flight to one site).
What it does NOT measure: real network latency, TLS handshakes, real page sizes or gzip savings (body bytes
here are the decoded size, an upper bound on transfer) or the cost of any site that renders with JavaScript.
"""

import argparse
import json
import random
import resource
import sys
import tempfile
import time
import zlib
from collections import defaultdict
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from radar.database import Database  # noqa: E402
from radar.models import ProductCriteria, WatchInput  # noqa: E402
from radar.politeness import DomainGate  # noqa: E402
from radar.providers.registry import Registry  # noqa: E402
from radar.providers.storepage import StorePageProvider  # noqa: E402
from radar.scheduler import Scheduler  # noqa: E402
from radar.service import add_watch  # noqa: E402

PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731 - a public address, so no DNS is needed


def product_page(url, price, kb):
    ld = {
        "@context": "https://schema.org", "@type": "Product", "name": "Producto de prueba", "sku": "SKU-" + url[-6:],
        "offers": {"@type": "Offer", "price": f"{price:.2f}", "priceCurrency": "PEN",
                   "availability": "https://schema.org/InStock", "itemCondition": "https://schema.org/NewCondition",
                   "seller": {"@type": "Organization", "name": "Tienda de prueba"}},
    }
    filler = "<!-- " + ("x" * max(0, kb * 1024 - 600)) + " -->"
    return ('<html><head><script type="application/ld+json">' + json.dumps(ld) + "</script></head><body>"
            + filler + "</body></html>")


def run(watches, domains, hours, page_kb, min_s, max_s, workers, price_change, seed, step=0.5):
    rng = random.Random(seed)
    clock = {"now": float(int(time.time()))}
    hosts = [f"shop{d}.test" for d in range(domains)]
    seen = defaultdict(list)  # host -> simulated request times
    in_flight = defaultdict(int)
    worst_overlap = [0]

    def handler(request):
        host = request.url.host
        in_flight[host] += 1
        worst_overlap[0] = max(worst_overlap[0], in_flight[host])
        try:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /\n", headers={"content-type": "text/plain"})
            seen[host].append(clock["now"])
            base = 50.0 + zlib.crc32(request.url.path.encode()) % 900
            price = base * (1 - 0.1) if rng.random() < price_change else base
            return httpx.Response(200, text=product_page(str(request.url), price, page_kb),
                                  headers={"content-type": "text/html; charset=utf-8"})
        finally:
            in_flight[host] -= 1

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    provider = StorePageProvider(hosts, contact="bench@example.org", client=client, resolver=PUBLIC)
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "bench.sqlite3")
        gate = DomainGate(db, gap=(15.0, 45.0), rng=rng)
        sched = Scheduler(db, Registry([provider]), gate, min_s=min_s, max_s=max_s, rng=rng,
                          clock=lambda: clock["now"], max_workers=workers)
        import os
        os.environ["RADAR_MAX_WATCHES"] = str(max(1000, watches))
        for i in range(watches):
            host = hosts[i % domains]
            add_watch(db, WatchInput(criteria=ProductCriteria(url=f"https://{host}/p/{i:06d}", currency="PEN"),
                                     provider="store", alert_mode="smart", category="tecnologia", subcategory="laptops"))
        start_cpu, start_wall = time.process_time(), time.time()
        for _ in range(int(hours * 3600 / step)):
            sched.tick(clock["now"])
            clock["now"] += step
        cpu, wall = time.process_time() - start_cpu, time.time() - start_wall
        with db.connect() as con:
            traffic = con.execute("SELECT COALESCE(SUM(requests),0) r, COALESCE(SUM(bytes_in),0) b FROM domain_state").fetchone()
            queries = con.execute("SELECT COUNT(*) FROM queries").fetchone()[0]
            observations = con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            alerts = con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
            incidents = con.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
            checked = con.execute("SELECT COUNT(DISTINCT watch_id) FROM queries").fetchone()[0]
        db_bytes = sum(p.stat().st_size for p in Path(tmp).glob("bench.sqlite3*"))
    gaps = [b - a for times in seen.values() for a, b in zip(times, times[1:])]
    sim_s = hours * 3600
    per_watch = [len(t) for t in seen.values()]
    day = 86400 / sim_s
    return {
        "watches": watches, "domains": domains, "simulated_hours": hours, "page_kb": page_kb,
        "configured_interval_s": [min_s, max_s], "tick_step_s": step,
        "requests_to_pages": sum(len(t) for t in seen.values()),
        "watches_checked": checked,
        "mean_interval_per_watch_s": round(sim_s * watches / max(1, sum(per_watch)), 1),
        "cpu_seconds": round(cpu, 2), "wall_seconds": round(wall, 2),
        "cpu_ms_per_request": round(cpu / max(1, traffic["r"]) * 1000, 2),
        "cpu_percent_of_one_core_if_real_time": round(cpu / sim_s * 100, 3),
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        "database_mb": round(db_bytes / 1048576, 2),
        "database_mb_per_day": round(db_bytes / 1048576 * day, 2),
        "queries_rows": queries, "observation_rows": observations, "alerts": alerts, "incidents": incidents,
        "requests_total_incl_robots": traffic["r"],
        "downloaded_mb": round(traffic["b"] / 1048576, 2),
        "downloaded_mb_per_day": round(traffic["b"] / 1048576 * day, 1),
        "min_gap_between_requests_to_one_site_s": round(min(gaps), 1) if gaps else None,
        "max_requests_in_flight_to_one_site": worst_overlap[0],
        "max_page_requests_per_site_per_day": round(max((len(t) for t in seen.values()), default=0) * day),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watches", type=int, default=200)
    ap.add_argument("--domains", type=int, default=10)
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--page-kb", type=int, default=90)
    ap.add_argument("--min-interval", type=float, default=300)
    ap.add_argument("--max-interval", type=float, default=900)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--price-change", type=float, default=0.02, help="share of requests that see a new price")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--step", type=float, default=0.5,
                    help="simulated seconds between scheduler ticks; the real loop wakes every 0.5-5 s")
    args = ap.parse_args()
    print(json.dumps(run(args.watches, args.domains, args.hours, args.page_kb, args.min_interval, args.max_interval,
                         args.workers, args.price_change, args.seed, args.step), indent=2))


if __name__ == "__main__":
    main()
