"""Resource usage of the monitor itself: CPU, memory, storage and bandwidth. Standard library only.

Bandwidth counts response BODY bytes per domain (what the monitor downloaded); TLS and header
overhead are not included, so real traffic is a few percent higher. CPU is this process's own
CPU time, not the machine's."""

import os
from pathlib import Path
import shutil
import time

STARTED = time.time()


def _rss_mb():
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError):
        pass
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(peak / 1024, 1)  # Linux reports kB (peak, not current)
    except (ImportError, OSError):
        return None


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def snapshot(db, now=None):
    now = time.time() if now is None else now
    uptime = max(1.0, now - STARTED)
    cpu = time.process_time()
    base = Path(db.path)
    db_bytes = _size(base) + _size(str(base) + "-wal") + _size(str(base) + "-shm")
    try:
        disk = shutil.disk_usage(base.parent)
        disk_free = disk.free
    except OSError:
        disk_free = None
    with db.connect() as con:
        traffic = con.execute("SELECT COALESCE(SUM(requests),0) r, COALESCE(SUM(bytes_in),0) b FROM domain_state").fetchone()
        top = [
            dict(r)
            for r in con.execute(
                "SELECT domain,requests,bytes_in,ROUND(latency_s,3) latency_s,ROUND(delay_s,2) delay_s,egress "
                "FROM domain_state ORDER BY bytes_in DESC LIMIT 10"
            )
        ]
        counts = {
            name: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for name, table in (("watches", "watches"), ("observations", "observations"),
                                ("queries", "queries"), ("catalogs", "catalogs"), ("incidents", "incidents"))
        }
        day = con.execute("SELECT COUNT(*) FROM incidents WHERE at>?", (now - 86400,)).fetchone()[0]
    return {
        "uptime_s": round(uptime),
        "cpu_seconds": round(cpu, 2),
        "cpu_percent_of_one_core": round(cpu / uptime * 100, 2),
        "rss_mb": _rss_mb(),
        "database_mb": round(db_bytes / 1048576, 3),
        "disk_free_gb": None if disk_free is None else round(disk_free / 1073741824, 2),
        "requests_total": traffic["r"],
        "downloaded_mb": round(traffic["b"] / 1048576, 3),
        "downloaded_mb_per_day": round(traffic["b"] / 1048576 / uptime * 86400, 2),
        "top_domains": top,
        "rows": counts,
        "incidents_24h": day,
        "note": "Ancho de banda = bytes de cuerpo descargados; no incluye cabeceras ni TLS.",
    }
