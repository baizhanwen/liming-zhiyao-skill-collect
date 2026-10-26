"""SQLite document entities with atomic writes and FTS5, no credentials."""
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager,closing


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get('SKILL_COLLECT_DATA', 'data')).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'app.sqlite3'
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS entities (
                kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(kind,id));
            CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(kind UNINDEXED,id UNINDEXED,text,tokenize='unicode61');
            CREATE TABLE IF NOT EXISTS migrations (version INTEGER PRIMARY KEY,applied_at TEXT);
            INSERT OR IGNORE INTO migrations VALUES(1,datetime('now'));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        try:
            db.execute('PRAGMA busy_timeout=20000')
            with db:
                yield db
        finally:
            db.close()

    def get(self, kind, id):
        with self.connect() as db:
            row = db.execute('SELECT data FROM entities WHERE kind=? AND id=?', (kind, id)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, kind):
        with self.connect() as db:
            rows = db.execute('SELECT data FROM entities WHERE kind=? ORDER BY rowid DESC', (kind,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def put(self, kind, value):
        value = dict(value)
        value.setdefault('id', uuid.uuid4().hex)
        with self.lock, self.connect() as db:
            old = db.execute('SELECT data FROM entities WHERE kind=? AND id=?', (kind, value['id'])).fetchone()
            value.setdefault('created_at', json.loads(old[0])['created_at'] if old else now())
            value['updated_at'] = now()
            db.execute('INSERT OR REPLACE INTO entities VALUES(?,?,?)', (kind, value['id'], json.dumps(value, ensure_ascii=False)))
            db.execute('DELETE FROM search WHERE kind=? AND id=?', (kind, value['id']))
            if kind in ('skills', 'knowledge'):
                content = ' '.join(str(value.get(k, '')) for k in ('title','name','description','summary','body','tags','category'))
                db.execute('INSERT INTO search(kind,id,text) VALUES(?,?,?)', (kind, value['id'], content))
        return value

    def delete(self, kind, id):
        with self.lock, self.connect() as db:
            db.execute('DELETE FROM entities WHERE kind=? AND id=?', (kind,id))
            db.execute('DELETE FROM search WHERE kind=? AND id=?', (kind,id))

    def find(self, kind, q='', tag='', category='', archived=False):
        # FTS5 indexes Latin tokens; substring fallback preserves Chinese keyword search.
        values = self.list(kind)
        if q:
            with self.connect() as db:
                ids = {r[0] for r in db.execute('SELECT id FROM search WHERE kind=? AND search MATCH ?', (kind, '"' + q.replace('"','""') + '"'))}
            values = [v for v in values if v['id'] in ids or q.casefold() in json.dumps(v, ensure_ascii=False).casefold()]
        return [v for v in values if bool(v.get('archived',False)) == archived and (not tag or tag in v.get('tags',[])) and (not category or v.get('category') == category)]

    def backup_to(self, target):
        with self.lock, self.connect() as source, closing(sqlite3.connect(target)) as dest:
            source.backup(dest)
