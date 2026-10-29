import sqlite3
from pathlib import Path
from contextlib import closing, contextmanager

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "agent.db"

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # NOTE: `with sqlite3.connect(...)` only commits on exit, it never closes the
    # handle. On Windows that leaves the file locked, so database files cannot be
    # deleted or backed up afterwards. Use `closing()` + an explicit commit.
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            source TEXT NOT NULL DEFAULT 'user',
            confidence REAL NOT NULL DEFAULT 1.0,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            goal TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'created',
            current_stage INTEGER NOT NULL DEFAULT 1,
            plan_json TEXT NOT NULL DEFAULT '[]',
            result TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            stage INTEGER NOT NULL,
            summary TEXT NOT NULL,
            state_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            event_type TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        # Additive migrations: preserve existing data and old databases.
        memory_columns = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
        if "tags_json" not in memory_columns:
            conn.execute("ALTER TABLE memories ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'")
        if "expires_at" not in memory_columns:
            conn.execute("ALTER TABLE memories ADD COLUMN expires_at TEXT")
        if "supersedes_id" not in memory_columns:
            conn.execute("ALTER TABLE memories ADD COLUMN supersedes_id INTEGER")
        if "conflict_group" not in memory_columns:
            conn.execute("ALTER TABLE memories ADD COLUMN conflict_group TEXT")
        conn.execute("CREATE TABLE IF NOT EXISTS memory_links ("
                     "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                     "source_memory_id INTEGER NOT NULL,"
                     "target_memory_id INTEGER NOT NULL,"
                     "relation TEXT NOT NULL,"
                     "note TEXT NOT NULL DEFAULT '',"
                     "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                     "FOREIGN KEY(source_memory_id) REFERENCES memories(id),"
                     "FOREIGN KEY(target_memory_id) REFERENCES memories(id))")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_status_updated ON memories(status, updated_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_task_stage ON snapshots(task_id, stage DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_task_created ON events(task_id, created_at DESC)")
        # Phase 4: persisted local embedding vectors; existing memory rows are untouched.
        conn.execute("""CREATE TABLE IF NOT EXISTS memory_embeddings (
            memory_id INTEGER PRIMARY KEY,
            model_name TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            dimension INTEGER NOT NULL,
            vector_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_embeddings_model ON memory_embeddings(model_name)")
        # ---- Phase 5: memory governance. Purely additive; old rows keep working. ----
        _migrate_phase5(conn)
        # ---- Phase 6: lifecycle types, feedback & consolidation candidates ----
        _migrate_phase6(conn)
        conn.commit()


# Each entry: (column, SQL type+constraints, backfill SQL or None)
PHASE5_MEMORY_COLUMNS = [
    ("importance_score", "REAL NOT NULL DEFAULT 50", None),
    ("confidence_score", "REAL NOT NULL DEFAULT 70",
     "UPDATE memories SET confidence_score = ROUND(MIN(100.0, MAX(0.0, COALESCE(confidence, 0.7) * 100.0)), 2) "
     "WHERE confidence_score IS NULL OR confidence_score = 70"),
    ("freshness_score", "REAL NOT NULL DEFAULT 70", None),
    ("access_count", "INTEGER NOT NULL DEFAULT 0", None),
    ("last_accessed_at", "TEXT", None),
    ("last_evaluated_at", "TEXT", None),
    ("memory_status", "TEXT NOT NULL DEFAULT 'active'", None),
    ("protected", "INTEGER NOT NULL DEFAULT 0", None),
]


def _add_column_if_missing(conn, table: str, column: str, ddl: str) -> bool:
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column in existing:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    return True


def _migrate_phase5(conn) -> dict:
    """Idempotent Phase-5 migration. Safe to run on every startup and repeatedly.

    `memories.status` stays the single authoritative retrieval filter
    (active / archived). `memories.memory_status` is a governance VIEW field
    (active / archived / superseded / pending_review) and is backfilled from
    status + supersedes links so old data is never lost or mislabelled.
    """
    applied: list[str] = []
    for column, ddl, backfill in PHASE5_MEMORY_COLUMNS:
        if _add_column_if_missing(conn, "memories", column, ddl):
            applied.append(f"memories.{column}")
        if backfill:
            try:
                conn.execute(backfill)
            except Exception:
                # Backfill must never block startup; scoring recomputes on demand.
                pass

    # Governance view status backfill for pre-Phase-5 rows.
    try:
        conn.execute("""
            UPDATE memories SET memory_status = CASE
                WHEN status = 'archived' THEN
                    CASE WHEN EXISTS (SELECT 1 FROM memory_links l
                                      WHERE l.target_memory_id = memories.id
                                        AND l.relation = 'supersedes')
                         THEN 'superseded' ELSE 'archived' END
                ELSE 'active' END
            WHERE memory_status IS NULL OR memory_status = '' OR memory_status = 'active'
        """)
    except Exception:
        pass

    conn.execute("""CREATE TABLE IF NOT EXISTS governance_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mode TEXT NOT NULL DEFAULT 'dry_run',
        status TEXT NOT NULL DEFAULT 'running',
        rule_version TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        finished_at TEXT,
        candidates_scanned INTEGER NOT NULL DEFAULT 0,
        actions_generated INTEGER NOT NULL DEFAULT 0,
        actions_applied INTEGER NOT NULL DEFAULT 0,
        actions_failed INTEGER NOT NULL DEFAULT 0,
        summary TEXT NOT NULL DEFAULT '',
        stats_json TEXT NOT NULL DEFAULT '{}'
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS governance_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER,
        action_type TEXT NOT NULL,
        memory_id INTEGER NOT NULL,
        target_memory_id INTEGER,
        old_value_json TEXT NOT NULL DEFAULT '{}',
        new_value_json TEXT NOT NULL DEFAULT '{}',
        reason TEXT NOT NULL DEFAULT '',
        rule_version TEXT NOT NULL DEFAULT '',
        approval_status TEXT NOT NULL DEFAULT 'pending',
        execution_status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        executed_at TEXT,
        error_message TEXT,
        rollback_info_json TEXT NOT NULL DEFAULT '{}',
        rolled_back_at TEXT,
        FOREIGN KEY(run_id) REFERENCES governance_runs(id),
        FOREIGN KEY(memory_id) REFERENCES memories(id),
        FOREIGN KEY(target_memory_id) REFERENCES memories(id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS memory_conflicts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER,
        memory_id INTEGER NOT NULL,
        candidate_memory_id INTEGER NOT NULL,
        conflict_type TEXT NOT NULL,
        similarity REAL,
        confidence REAL NOT NULL DEFAULT 0.0,
        reason TEXT NOT NULL DEFAULT '',
        recommendation TEXT NOT NULL DEFAULT '',
        resolution TEXT NOT NULL DEFAULT 'pending',
        resolved_by TEXT NOT NULL DEFAULT '',
        resolved_at TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        rule_version TEXT NOT NULL DEFAULT '',
        FOREIGN KEY(run_id) REFERENCES governance_runs(id),
        FOREIGN KEY(memory_id) REFERENCES memories(id),
        FOREIGN KEY(candidate_memory_id) REFERENCES memories(id),
        UNIQUE(memory_id, candidate_memory_id, conflict_type)
    )""")
    # Rows inserted after the migration cannot inherit `confidence` through a
    # column DEFAULT, so keep the two in sync with a trigger that only fires on
    # the untouched default value (never overwrites a scored value).
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_memories_confidence_score
        AFTER INSERT ON memories
        BEGIN
            UPDATE memories
               SET confidence_score = ROUND(MIN(100.0, MAX(0.0,
                       COALESCE(NEW.confidence, 0.7) * 100.0)), 2)
             WHERE id = NEW.id AND confidence_score = 70;
        END
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_gov_actions_run ON governance_actions(run_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gov_actions_status ON governance_actions(approval_status, execution_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gov_actions_memory ON governance_actions(memory_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_conflicts_memory ON memory_conflicts(memory_id, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_conflicts_resolution ON memory_conflicts(resolution)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_gov_status ON memories(memory_status)")
    return {"applied_columns": applied}


# ---- Phase 6: lifecycle types, unified feedback & consolidation candidates ----
# Purely additive. Old rows keep memory_type = NULL (readable/searchable).
PHASE6_MEMORY_COLUMNS = [
    # memory_type is nullable; untyped legacy data stays searchable and is
    # surfaced as "unknown" by the recall layer rather than forced into a bucket.
    ("memory_type", "TEXT", None),
]


def _migrate_phase6(conn) -> dict:
    """Idempotent Phase-6 migration. Safe to run on every startup and repeatedly.

    Adds:
      * memories.memory_type   (nullable; episodic/semantic/preference/procedural/goal/reflection)
      * memory_feedback        (retrieved/used/helpful/not_relevant/corrected counters, one row per memory)
      * consolidation_candidates (auto-generated merge/extract candidates, NOT applied to facts)

    Never drops columns, never rewrites existing rows, never removes old data.
    """
    applied: list[str] = []
    for column, ddl, backfill in PHASE6_MEMORY_COLUMNS:
        if _add_column_if_missing(conn, "memories", column, ddl):
            applied.append(f"memories.{column}")
        if backfill:
            try:
                conn.execute(backfill)
            except Exception:
                pass

    # One feedback row per memory; created lazily but the table must exist.
    conn.execute("""CREATE TABLE IF NOT EXISTS memory_feedback (
        memory_id INTEGER PRIMARY KEY,
        retrieved_count INTEGER NOT NULL DEFAULT 0,
        used_count INTEGER NOT NULL DEFAULT 0,
        helpful_count INTEGER NOT NULL DEFAULT 0,
        not_relevant_count INTEGER NOT NULL DEFAULT 0,
        corrected_count INTEGER NOT NULL DEFAULT 0,
        last_retrieved_at TEXT,
        last_used_at TEXT,
        last_feedback_at TEXT,
        history_json TEXT NOT NULL DEFAULT '[]',
        FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
    )""")

    conn.execute("""CREATE TABLE IF NOT EXISTS consolidation_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,                 -- 'merge_duplicate' | 'extract_semantic' | 'extract_procedural' | 'stale_fact' | 'missing_source' | 'conflict_review' | 'archive_review'
        status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | applied | rejected | skipped
        title TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        source_memory_ids TEXT NOT NULL DEFAULT '[]',  -- JSON array of contributing memory ids
        target_memory_id INTEGER,          -- populated only after approval+application
        confidence REAL NOT NULL DEFAULT 0.0,
        rule_version TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        resolved_at TEXT,
        resolved_by TEXT NOT NULL DEFAULT '',
        notes_json TEXT NOT NULL DEFAULT '{}',
        FOREIGN KEY(target_memory_id) REFERENCES memories(id)
    )""")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_memory ON memory_feedback(memory_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_consol_kind_status ON consolidation_candidates(kind, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_consol_status_created ON consolidation_candidates(status, created_at DESC)")
    return {"applied_columns": applied}


def init_db_phase5() -> dict:
    """Run the Phase-5 migration on the real database. Idempotent."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        result = _migrate_phase5(conn)
        conn.commit()
        return result

@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction():
    """Explicit atomic transaction for multi-table governance operations.

    Unlike `connect()`, any exception rolls the whole unit back instead of
    committing whatever happened to succeed. `connect()` keeps its original
    behaviour so Phases 1-4 are unaffected.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN")
    try:
        yield conn
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()

def rowdict(row):
    return dict(row) if row else None
