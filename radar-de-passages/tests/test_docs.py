"""The connector example in docs/conectores.md must keep working with the real pipeline."""

from pathlib import Path
import re
import time
import httpx
from radar.providers.registry import Registry
from radar.service import add_watch, refresh
from conftest import bus_watch

DOC = Path(__file__).resolve().parents[1] / "docs/conectores.md"


def load_example():
    block = re.search(r"```python\n(# conector-ejemplo\n.*?)```", DOC.read_text(), re.S).group(1)
    namespace = {}
    exec(compile(block, "docs/conectores.md", "exec"), namespace)  # noqa: S102 - our own documentation
    return namespace["MiConectorBus"]


def client(payload=None, status=200):
    payload = payload if payload is not None else {"trips": [
        {"id": "T1", "price": "45.50", "operator": "Cruz", "class": "semicama", "refundable": "no"},
        {"id": "T2", "price": "60", "operator": None, "class": "cama", "refundable": "si"},
    ]}
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=payload)))


def test_documented_connector_runs_through_the_real_pipeline(db):
    connector = load_example()(client())
    registry = Registry([connector])
    value = bus_watch().model_copy(update={"provider": "mi-bus"})
    key = add_watch(db, value)
    out = refresh(db, key, registry=registry, now=time.time())
    assert out["status"] == "success" and out["synthetic"] is False
    assert len(out["observations"]) == 2
    with db.connect() as con:
        rows = {r["total_cents"]: r["comparison_key"] for r in con.execute("SELECT total_cents,comparison_key FROM observations")}
    assert rows[4550] is not None   # all required conditions known -> comparable
    assert rows[6000] is None       # operator unknown stays unknown -> not comparable


def test_documented_connector_errors_drive_the_scheduler(db):
    for status, expected in ((403, "blocked"), (429, "rate_limited"), (500, "provider")):
        value = bus_watch(origin=f"Lima{status}").model_copy(update={"provider": "mi-bus"})
        connector = load_example()(client(status=status))
        registry = Registry([connector])
        key = add_watch(db, value)
        out = refresh(db, key, registry=registry, now=time.time())
        assert out["error_kind"] == expected


# --- the documents must not drift from the code --------------------------------------------------------------
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def documents():
    return [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")), *sorted((ROOT / "evidence").glob("*.md"))]


def test_every_relative_link_in_the_documents_points_to_a_file_that_exists():
    broken = []
    for doc in documents():
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", doc.read_text(encoding="utf-8")):
            if re.match(r"[a-z][a-z0-9+.-]*:", target):  # https:, mailto: ...
                continue
            if not (doc.parent / target).resolve().exists():
                broken.append(f"{doc.relative_to(ROOT)} -> {target}")
    assert broken == []


def test_every_environment_variable_the_code_reads_is_in_the_env_template_and_the_other_way_round():
    code = "\n".join(p.read_text(encoding="utf-8") for p in [*(ROOT / "radar").rglob("*.py"), ROOT / "run.py"])
    used = set(re.findall(r"""["'](RADAR_[A-Z0-9_]+)["']""", code))
    documented = set(re.findall(r"^#?\s*(RADAR_[A-Z0-9_]+)=", (ROOT / ".env.example").read_text(encoding="utf-8"), flags=re.M))
    dynamic = {n for n in documented if n.startswith(("RADAR_BUDGET_", "RADAR_EGRESS_PROXY_"))}  # built from a provider/profile name
    assert used - documented == set(), "the code reads variables that .env.example does not mention"
    assert documented - used - dynamic == set(), ".env.example documents variables nothing reads"


def test_the_removed_flight_provider_is_not_mentioned_anywhere_except_the_decision_log():
    scanned = [ROOT / "README.md", ROOT / "Dockerfile", ROOT / "docker-compose.yml", ROOT / "fly.toml", ROOT / ".env.example"]
    scanned += [p for p in (ROOT / "docs").glob("*.md") if p.name != "auditoria.md"]
    scanned += sorted((ROOT / "evidence").glob("*.md")) + sorted((ROOT / "sources").glob("*.json"))
    scanned += sorted((ROOT / "deploy").glob("*")) + sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    scanned += sorted((ROOT / "scripts").glob("*.py")) + [ROOT / "static" / "app.js", ROOT / "static" / "index.html"]
    offenders = [str(p.relative_to(ROOT)) for p in scanned if "duffel" in p.read_text(encoding="utf-8").lower()]
    assert offenders == []


def test_the_shipped_catalogue_numbers_quoted_in_the_documents_are_true():
    from radar.sources import load_sources

    entries = list(load_sources().values())
    stores = [e for e in entries if e["kind"] == "store"]
    travel = [e for e in entries if e["kind"] != "store"]
    assert (len(entries), len(stores), len(travel)) == (158, 91, 67)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "158 fuentes" in readme and "91 tiendas" in readme
    by_kind = {k: sum(e["kind"] == k for e in travel) for k in {e["kind"] for e in travel}}
    assert by_kind == {"airline": 40, "bus": 17, "agency": 10}
