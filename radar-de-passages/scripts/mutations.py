"""Mutation check: break one safeguard on purpose and prove the tests notice.

Each mutation edits ONE spot of a throwaway copy of the project and runs only the tests meant to guard it.
    killed    -> at least one test failed (the safeguard is really being checked)
    SURVIVED  -> the tests still pass with the safeguard broken (a gap in the tests)
    n/a       -> the text to change was not found (this script is out of date)
The project itself is never modified.

    python scripts/mutations.py                      # all of them; prints a table and writes evidence/mutations.md
    python scripts/mutations.py --only M03 --skip-baseline
    python scripts/mutations.py --only M01,M02 --results a.json   # in batches ...
    python scripts/mutations.py --report a.json,b.json            # ... then one report
"""

import argparse
import concurrent.futures as futures
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PRIORITY = ["tests/test_priority.py", "tests/test_api_v3.py"]
RESTRICT = ["tests/test_restrictions.py", "tests/test_scheduler.py"]
STORE = ["tests/test_providers.py", "tests/test_restrictions.py", "tests/test_robots.py", "tests/test_discovery.py"]

# id, what is broken, file, text to find (must be unique), replacement, tests that should notice
MUTATIONS = [
    ("M01", "«máxima» ya no exige ser el mínimo histórico", "radar/priority.py",
     'if tier == "maxima" and price > (stats.get("minimum_cents") or price):', "if False:", PRIORITY),
    ("M02", "una sola comparación entre tiendas puede dar «máxima»", "radar/priority.py",
     'tier = "alta"  # cross-store alone never reaches the top tier', "pass", PRIORITY),
    ("M03", "otra tienda más barata ya no baja el nivel", "radar/priority.py",
     'if out["best_peer_cents"] is not None and price > out["best_peer_cents"] * (1 + th.peer_gap / 100):', "if False:", PRIORITY),
    ("M04", "se asigna prioridad sin historial suficiente", "radar/priority.py",
     "if sightings >= th.min_sightings and span >= th.min_span_s:", "if True:", PRIORITY),
    ("M05", "el umbral «baja» pasa de ≥ a >", "radar/priority.py",
     "if drop_percent >= getattr(th, name):", "if drop_percent > getattr(th, name):", PRIORITY),
    ("M06", "una prioridad mayor ya no rompe el enfriamiento de 30 min", "radar/rules.py",
     "escalates = rank > last_rank and age < ESCALATION_WINDOW_S", "escalates = False", PRIORITY + ["tests/test_radar.py"]),
    ("M07", "se quita el enfriamiento de 30 min", "radar/rules.py",
     "in_cooldown = age < COOLDOWN_S", "in_cooldown = False", PRIORITY + ["tests/test_radar.py"]),
    ("M08", "Telegram entrega por antigüedad y no por prioridad", "radar/notifications.py",
     "ORDER BY priority_rank DESC, created_at LIMIT 100", "ORDER BY created_at LIMIT 100", ["tests/test_cli_delivery.py", "tests/test_priority.py"]),
    ("M09", "un error HTTP puede acelerar el ritmo (AutoThrottle)", "radar/politeness.py",
     "delay = max(delay, previous)  # errors may slow us down, never speed us up", "pass", RESTRICT),
    ("M10", "un rechazo (CAPTCHA, 403, 429...) ya no pausa el sitio", "radar/scheduler.py",
     "if kind in PAUSE_KINDS:", "if False:", RESTRICT),
    ("M11", "una salida apagada cae a la salida directa", "radar/egress.py",
     "        profile = self.profile_for(domain)\n        if not profile.enabled:\n            raise EgressDisabled(",
     "        profile = self.profile_for(domain)\n        if not profile.enabled:\n            profile = self.profiles[self.default]\n        if not profile.enabled:\n            raise EgressDisabled(",
     RESTRICT),
    ("M12", "se acepta una tienda fuera de la lista permitida", "radar/providers/storepage.py",
     "        if not self.allows(host):\n            raise ProviderError(\n                \"Dominio no permitido", "        if False:\n            raise ProviderError(\n                \"Dominio no permitido", STORE),
    ("M13", "se acepta un dominio que resuelve a una IP privada (SSRF)", "radar/providers/storepage.py",
     "if not is_public_address(address):", "if False:", STORE),
    ("M14", "se interpreta cualquier tipo de contenido", "radar/providers/storepage.py",
     "and ctype not in CONTENT_TYPES[expect]:", "and False:", STORE),
    ("M15", "un 401/403 en robots.txt se toma como «sin reglas»", "radar/providers/storepage.py",
     "if status in (401, 403):", "if False:", STORE),
    ("M16", "robots.txt: gana la regla más corta en vez de la más larga", "radar/robots.py",
     "score = (len(pattern), allow)  # longest wins; on a tie Allow beats Disallow",
     "score = (-len(pattern), allow)", ["tests/test_robots.py"]),
    ("M17", "robots.txt: en empate gana Disallow en vez de Allow", "radar/robots.py",
     "score = (len(pattern), allow)  # longest wins; on a tie Allow beats Disallow",
     "score = (len(pattern), not allow)", ["tests/test_robots.py"]),
    ("M18", "los registros dejan de borrar credenciales", "radar/security.py",
     'return CREDENTIAL.sub("***REDACTED***", text)', "return text", ["tests/test_restrictions.py", "tests/test_flights.py"]),
    ("M19", "dos instancias pueden vigilar a la vez (lease)", "radar/scheduler.py",
     "if not self.hold_lease(now):", "if False:", ["tests/test_scheduler.py"]),
]


def run_one(mutation, python):
    mid, what, rel, old, new, tests = mutation
    with tempfile.TemporaryDirectory(prefix=f"mut-{mid}-") as tmp:
        work = Path(tmp) / "p"
        shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(
            "node_modules", ".git", "__pycache__", "*.sqlite3*", "evidence", ".pytest_cache", "runtime"))
        target = work / rel
        text = target.read_text(encoding="utf-8")
        if text.count(old) != 1:
            return mid, what, rel, "n/a", f"texto encontrado {text.count(old)} veces"
        target.write_text(text.replace(old, new), encoding="utf-8")
        run = subprocess.run([python, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *tests],
                             cwd=work, capture_output=True, text=True, timeout=900)
        tail = (run.stdout.strip().splitlines() or [""])[-1]
        failed = next((ln for ln in run.stdout.splitlines() if ln.startswith("FAILED")), "")
        if run.returncode == 0:
            return mid, what, rel, "SURVIVED", tail
        return mid, what, rel, "killed", failed.replace("FAILED ", "")[:110] or tail


def baseline(python, groups):
    for tests in groups:
        run = subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests], cwd=ROOT,
                             capture_output=True, text=True, timeout=900)
        if run.returncode != 0:
            sys.exit("La línea base falla; no tiene sentido mutar:\n" + run.stdout[-1500:])


def table(results):
    rows = ["| Id | Salvaguarda que se rompe | Archivo | Resultado | Prueba que la detecta |", "|---|---|---|---|---|"]
    for mid, what, rel, verdict, detail in results:
        rows.append(f"| {mid} | {what} | `{rel}` | **{verdict}** | {str(detail).replace('|', '/')} |")
    killed = sum(r[3] == "killed" for r in results)
    return "\n".join(rows), f"{killed} de {len(results)} mutaciones detectadas por las pruebas.", killed


def write_report(results, out):
    rows, summary, _ = table(sorted(results))
    Path(out).parent.mkdir(exist_ok=True)
    Path(out).write_text(
        "# Comprobación por mutaciones\n\nSe rompe **una** salvaguarda a propósito en una copia temporal del proyecto y se "
        "comprueba que las pruebas lo notan (`python scripts/mutations.py`). `killed` = una prueba falló (bien); "
        "`SURVIVED` = las pruebas siguen pasando con la salvaguarda rota (un hueco).\n\n" + rows + f"\n\n**{summary}**\n",
        encoding="utf-8")


def main():
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="ids separados por coma, p. ej. M03,M10")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--skip-baseline", action="store_true", help="la suite completa ya se ejecutó")
    ap.add_argument("--results", help="guarda los resultados de esta tanda en este JSON")
    ap.add_argument("--report", help="JSON de tandas separados por coma: escribe evidence/mutations.md con todos")
    ap.add_argument("--out", default=str(ROOT / "evidence" / "mutations.md"))
    args = ap.parse_args()
    if args.report:
        latest = {}  # a later batch replaces an earlier result for the same id (a test was added, then re-run)
        for path in args.report.split(","):
            for r in json.loads(Path(path).read_text()):
                latest[r[0]] = tuple(r)
        merged = list(latest.values())
        write_report(merged, args.out)
        print(table(sorted(merged))[1])
        return
    chosen = [m for m in MUTATIONS if not args.only or m[0] in args.only.split(",")]
    if not args.skip_baseline:
        baseline(sys.executable, {tuple(m[5]) for m in chosen})
    with futures.ThreadPoolExecutor(args.jobs) as pool:
        results = list(pool.map(lambda m: run_one(m, sys.executable), chosen))
    rows, summary, killed = table(results)
    print(rows + "\n\n" + summary)
    if args.results:
        Path(args.results).write_text(json.dumps(results))
    elif not args.only:
        write_report(results, args.out)
    sys.exit(0 if killed == len(results) else 1)


if __name__ == "__main__":
    main()
