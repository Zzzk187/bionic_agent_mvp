"""Offline tests for vector storage/matching primitives (does not download the model)."""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db
from app.embeddings import cosine_similarity, content_hash

def run():
    assert abs(cosine_similarity([1, 0], [1, 0]) - 1.0) < 1e-9
    assert abs(cosine_similarity([1, 0], [0, 1])) < 1e-9
    assert cosine_similarity([1], [1, 2]) == 0.0
    original = db.DB_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            db.DB_PATH = Path(td) / 'legacy.db'
            # Simulate a pre-Phase-4 database, then migrate it in place.
            with db.connect() as conn:
                conn.execute('CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, category TEXT NOT NULL DEFAULT \'general\', source TEXT NOT NULL DEFAULT \'user\', confidence REAL NOT NULL DEFAULT 1.0, status TEXT NOT NULL DEFAULT \'active\', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
                conn.execute("INSERT INTO memories(content) VALUES(?)", ('老数据库中的记忆',))
            db.init_db()
            with db.connect() as conn:
                row = conn.execute('SELECT * FROM memories WHERE id=1').fetchone()
                assert row['content'] == '老数据库中的记忆'
                assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memory_embeddings'").fetchone()
                conn.execute('INSERT INTO memory_embeddings(memory_id,model_name,content_hash,dimension,vector_json) VALUES(?,?,?,?,?)',
                             (1, 'test-model', content_hash(row['content']), 2, json.dumps([1.0, 0.0])))
                vec = json.loads(conn.execute('SELECT vector_json FROM memory_embeddings WHERE memory_id=1').fetchone()[0])
                assert vec == [1.0, 0.0]
        print('向量数学、旧 SQLite 兼容迁移与向量持久化测试通过')
    finally:
        db.DB_PATH = original

if __name__ == '__main__': run()
