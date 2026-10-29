"""Offline smoke tests for the database/tool logic; run from project root."""
import json
import tempfile
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db

def run():
    original = db.DB_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            db.DB_PATH = Path(td) / "test.db"
            db.init_db()
            with db.connect() as conn:
                # Existing schema migration and baseline tables are valid.
                cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
                assert {"tags_json", "expires_at"} <= cols
                cur = conn.execute(
                    "INSERT INTO memories(content,category,source,confidence,tags_json) VALUES(?,?,?,?,?)",
                    ("测试记忆", "test", "smoke_test", 0.9, json.dumps(["tag"], ensure_ascii=False))
                )
                mid = cur.lastrowid
                row = conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
                assert row["content"] == "测试记忆"
                assert json.loads(row["tags_json"]) == ["tag"]
            print("数据库迁移与记忆读写烟雾测试通过")
    finally:
        db.DB_PATH = original

if __name__ == "__main__":
    run()
