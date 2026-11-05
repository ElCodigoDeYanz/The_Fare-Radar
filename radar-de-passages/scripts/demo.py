"""Synthetic, reproducible journey against real API and pipeline with a temp DB."""

from datetime import date, timedelta
import json
from pathlib import Path
import sys
import tempfile
import time
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from radar.api import create_app
from radar.models import Criteria, WatchInput
from radar.service import add_watch, refresh


def demo():
    now = time.time()
    with tempfile.TemporaryDirectory() as folder:
        app = create_app(Path(folder) / "demo.db")
        db = app.state.db
        key = add_watch(
            db,
            WatchInput(
                criteria=Criteria(
                    origin="LIM",
                    destination="MAD",
                    departure=(date.today() + timedelta(days=45)).isoformat(),
                ),
                target_cents=35000,
            ),
        )
        outcomes = [
            refresh(db, key, scenario, now=now + i * 6)
            for i, scenario in enumerate(
                (
                    "baseline",
                    "drop",
                    "drop",
                    "failure",
                    "expired",
                    "different_conditions",
                    "missing_conditions",
                )
            )
        ]
        with TestClient(app) as client:
            history = client.get(f"/api/watches/{key}/history").json()
            alerts = client.get("/api/alerts").json()
            assert len(alerts) == 1 and alerts[0]["status"] == "simulated"
            assert history["query_count"] == 7 and history["observation_count"] == 5
            assert (
                outcomes[3]["status"] == "error"
                and outcomes[4]["observations"][0]["expired"]
            )
            return {
                "synthetic": True,
                "live_flight_connections": False,
                "telegram_messages_sent": 0,
                "transport": "TestClient for API evidence; service clock explicitly advanced without sleep",
                "results": outcomes,
                "history": history,
                "alerts": alerts,
            }


if __name__ == "__main__":
    result = demo()
    (ROOT / "evidence/demo-results.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    )
    print(
        json.dumps(
            {
                "queries": result["history"]["query_count"],
                "observations": result["history"]["observation_count"],
                "alerts": len(result["alerts"]),
                "synthetic": True,
            },
            indent=2,
        )
    )
