"""Persistence only. The query journal is append-only, including failures.

Schema versions (PRAGMA user_version):
  1  original flight-only demo
  2  watch kinds/providers/categories, scheduler state, per-domain politeness state,
     provider usage budget, error kinds in the query journal
  3  alert modes and priorities, catalogs (discovery by category/sitemap), cross-store
     product identity (match_key)
"""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
 id TEXT PRIMARY KEY,
 kind TEXT NOT NULL DEFAULT 'flight' CHECK(kind IN ('flight','bus','product')),
 provider TEXT NOT NULL DEFAULT 'demo',
 category TEXT, subcategory TEXT, label TEXT,
 criteria_json TEXT NOT NULL,
 target_cents INTEGER NOT NULL CHECK(target_cents>0), enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 channel TEXT NOT NULL CHECK(channel IN ('simulation','telegram')), created_at REAL NOT NULL,
 UNIQUE(provider, criteria_json)
);
CREATE TABLE IF NOT EXISTS queries (
 id TEXT PRIMARY KEY, watch_id TEXT NOT NULL REFERENCES watches(id),
 started_at REAL NOT NULL, finished_at REAL,
 status TEXT NOT NULL CHECK(status IN ('running','success','error')), error TEXT,
 error_kind TEXT
);
CREATE INDEX IF NOT EXISTS watch_queries ON queries(watch_id,started_at DESC);
CREATE TABLE IF NOT EXISTS observations (
 id TEXT PRIMARY KEY, watch_id TEXT NOT NULL REFERENCES watches(id),
 provider TEXT NOT NULL, provider_observation_id TEXT NOT NULL,
 observed_at REAL NOT NULL, expires_at REAL NOT NULL, currency TEXT NOT NULL,
 total_cents INTEGER NOT NULL CHECK(total_cents>0), criteria_json TEXT NOT NULL,
 conditions_json TEXT NOT NULL, comparison_key TEXT, source TEXT NOT NULL,
 synthetic INTEGER NOT NULL CHECK(synthetic IN (0,1)),
 UNIQUE(watch_id,provider,provider_observation_id)
);
CREATE INDEX IF NOT EXISTS observation_compare ON observations(watch_id,comparison_key,observed_at DESC);
CREATE INDEX IF NOT EXISTS observation_time ON observations(watch_id,observed_at DESC);
CREATE TABLE IF NOT EXISTS query_observations (
 query_id TEXT NOT NULL REFERENCES queries(id), observation_id TEXT NOT NULL REFERENCES observations(id),
 PRIMARY KEY(query_id,observation_id)
);
CREATE TABLE IF NOT EXISTS alerts (
 id TEXT PRIMARY KEY, watch_id TEXT NOT NULL REFERENCES watches(id),
 observation_id TEXT NOT NULL REFERENCES observations(id), created_at REAL NOT NULL,
 channel TEXT NOT NULL CHECK(channel IN ('simulation','telegram')),
 status TEXT NOT NULL CHECK(status IN ('simulated','pending','sending','sent','uncertain','failed')),
 message TEXT NOT NULL, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 UNIQUE(watch_id,observation_id)
);
CREATE TABLE IF NOT EXISTS watch_schedule (
 watch_id TEXT PRIMARY KEY REFERENCES watches(id),
 next_run_at REAL NOT NULL, fail_count INTEGER NOT NULL DEFAULT 0,
 last_run_at REAL, last_status TEXT
);
CREATE INDEX IF NOT EXISTS schedule_due ON watch_schedule(next_run_at);
CREATE TABLE IF NOT EXISTS domain_state (
 domain TEXT PRIMARY KEY,
 next_allowed_at REAL NOT NULL DEFAULT 0,
 blocked_until REAL NOT NULL DEFAULT 0,
 block_count INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'ok' CHECK(status IN ('ok','cooling','disabled')),
 last_error TEXT, updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scheduler_lease (
 id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_usage (
 provider TEXT NOT NULL, month TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(provider, month)
);
"""

# v1 -> v2. The watches table is rebuilt because its UNIQUE constraint changes
# (criteria alone -> provider + criteria). Foreign keys are off during the copy.
MIGRATE_1_TO_2 = """
CREATE TABLE watches_v2 (
 id TEXT PRIMARY KEY,
 kind TEXT NOT NULL DEFAULT 'flight' CHECK(kind IN ('flight','bus','product')),
 provider TEXT NOT NULL DEFAULT 'demo',
 category TEXT, subcategory TEXT, label TEXT,
 criteria_json TEXT NOT NULL,
 target_cents INTEGER NOT NULL CHECK(target_cents>0), enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 channel TEXT NOT NULL CHECK(channel IN ('simulation','telegram')), created_at REAL NOT NULL,
 UNIQUE(provider, criteria_json)
);
INSERT INTO watches_v2(id,kind,provider,category,subcategory,label,criteria_json,target_cents,enabled,channel,created_at)
 SELECT id,'flight','demo','viajes','aereo',NULL,
  criteria_json,
  target_cents,enabled,channel,created_at FROM watches;
DROP TABLE watches;
ALTER TABLE watches_v2 RENAME TO watches;
ALTER TABLE queries ADD COLUMN error_kind TEXT;
"""

# v2 -> v3 (additive only: no table is rebuilt, nothing is rewritten).
# alert_mode: 'target' (absolute target, the original behaviour), 'smart' (priority tiers
# against the product's own history and its price in other stores) or 'both'.
MIGRATE_2_TO_3 = """
ALTER TABLE watches ADD COLUMN alert_mode TEXT NOT NULL DEFAULT 'target';
ALTER TABLE watches ADD COLUMN catalog_id TEXT;
ALTER TABLE watches ADD COLUMN title TEXT;
ALTER TABLE watches ADD COLUMN match_key TEXT;
CREATE INDEX IF NOT EXISTS watch_match ON watches(match_key);
CREATE INDEX IF NOT EXISTS watch_catalog ON watches(catalog_id);
ALTER TABLE alerts ADD COLUMN priority TEXT;
ALTER TABLE alerts ADD COLUMN priority_rank INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS catalogs (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL, provider TEXT NOT NULL DEFAULT 'store',
 domain TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('listing','sitemap')),
 url TEXT NOT NULL, category TEXT, subcategory TEXT, currency TEXT NOT NULL,
 include_regex TEXT, rules_json TEXT, max_items INTEGER NOT NULL CHECK(max_items>0), store_max INTEGER NOT NULL DEFAULT 40,
 alert_mode TEXT NOT NULL DEFAULT 'smart', channel TEXT NOT NULL DEFAULT 'simulation',
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 next_run_at REAL NOT NULL DEFAULT 0, fail_count INTEGER NOT NULL DEFAULT 0,
 last_run_at REAL, last_status TEXT, last_error TEXT, found_count INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL,
 UNIQUE(source_id, mode, url)
);
CREATE INDEX IF NOT EXISTS catalog_due ON catalogs(enabled,next_run_at);
ALTER TABLE queries ADD COLUMN egress TEXT;
ALTER TABLE queries ADD COLUMN http_status INTEGER;
ALTER TABLE domain_state ADD COLUMN requests INTEGER NOT NULL DEFAULT 0;
ALTER TABLE domain_state ADD COLUMN bytes_in INTEGER NOT NULL DEFAULT 0;
ALTER TABLE domain_state ADD COLUMN latency_s REAL;
ALTER TABLE domain_state ADD COLUMN delay_s REAL NOT NULL DEFAULT 0;
ALTER TABLE domain_state ADD COLUMN fail_streak INTEGER NOT NULL DEFAULT 0;
ALTER TABLE domain_state ADD COLUMN egress TEXT;
CREATE TABLE IF NOT EXISTS incidents (
 id TEXT PRIMARY KEY, at REAL NOT NULL, domain TEXT NOT NULL, provider TEXT,
 watch_id TEXT, catalog_id TEXT, url TEXT, http_status INTEGER,
 kind TEXT NOT NULL, severity TEXT NOT NULL CHECK(severity IN ('low','medium','high')),
 action TEXT NOT NULL, egress TEXT, detail TEXT NOT NULL,
 notified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS incident_time ON incidents(at DESC);
CREATE INDEX IF NOT EXISTS incident_domain ON incidents(domain,at DESC)
"""


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self):
        con = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            version, has_tables = self._state(con)
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"La base de datos es de una versión más nueva ({version}).")
            if has_tables and version == 1:
                self._migrate_1_to_2(con)
            con.execute("BEGIN IMMEDIATE")
            try:
                version = con.execute("PRAGMA user_version").fetchone()[0]
                if version < 2:  # brand-new database
                    for statement in _split(SCHEMA):
                        con.execute(statement)
                    version = 2
                if version == 2:
                    for statement in _split(MIGRATE_2_TO_3):
                        con.execute(statement)
                    con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
        finally:
            con.close()

    @staticmethod
    def _state(con):
        version = con.execute("PRAGMA user_version").fetchone()[0]
        has_tables = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='watches'"
        ).fetchone()[0]
        return version, has_tables

    @staticmethod
    def _migrate_1_to_2(con):
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("BEGIN IMMEDIATE")
        try:
            if con.execute("PRAGMA user_version").fetchone()[0] == 1:
                for statement in _split(MIGRATE_1_TO_2):
                    con.execute(statement)
                # Re-serialise criteria canonically with the fields added in v2, so
                # duplicate detection keeps working for migrated watches.
                for key, raw in con.execute("SELECT id,criteria_json FROM watches").fetchall():
                    data = json.loads(raw)
                    data.setdefault("kind", "flight")
                    data.setdefault("max_connections", 1)
                    data.setdefault("airlines", [])
                    con.execute(
                        "UPDATE watches SET criteria_json=? WHERE id=?",
                        (json.dumps(data, sort_keys=True, separators=(",", ":")), key),
                    )
                for statement in _split(SCHEMA):
                    con.execute(statement)
                bad = con.execute("PRAGMA foreign_key_check").fetchall()
                if bad:
                    raise RuntimeError("La migración dejaría referencias inválidas.")
                con.execute("PRAGMA user_version=2")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=20)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()


def _split(script):
    """Split a script into single statements (no semicolons inside our literals)."""
    return [s.strip() for s in script.split(";") if s.strip()]
