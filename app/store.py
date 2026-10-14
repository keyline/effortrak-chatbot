import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

from app.auth import Identity


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, company_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL, policy_key TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS reports (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    filename TEXT NOT NULL, format TEXT NOT NULL, payload BLOB NOT NULL,
                    employee_ids TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(messages)')}
            for column in ('model_content', 'query_context'):
                if column not in columns:
                    db.execute(f'ALTER TABLE messages ADD COLUMN {column} TEXT')

    @contextmanager
    def connect(self):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            yield db

    def assert_owner(self, conversation_id: str, identity: Identity):
        with self.connect() as db:
            row = db.execute(
                'SELECT * FROM conversations WHERE id=? AND company_id=? AND user_id=?',
                (conversation_id, identity.company_id, identity.user_id),
            ).fetchone()
        if row is None:
            raise HTTPException(404, 'Conversation not found.')
        if row['policy_key'] != identity.policy_key:
            raise HTTPException(409, 'Permissions changed. Start a new conversation.')

    def create(self, identity: Identity) -> str:
        conversation_id = str(uuid4())
        with self.connect() as db:
            db.execute('INSERT INTO conversations VALUES (?,?,?,?)',
                       (conversation_id, identity.company_id, identity.user_id, identity.policy_key))
        return conversation_id

    def history(self, conversation_id: str, identity: Identity) -> list[dict]:
        self.assert_owner(conversation_id, identity)
        with self.connect() as db:
            rows = db.execute(
                'SELECT role,content,created_at,model_content,query_context FROM messages WHERE conversation_id=? ORDER BY id',
                (conversation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def conversations(self, identity: Identity) -> list[dict]:
        with self.connect() as db:
            rows = db.execute('''SELECT c.id AS conversation_id, MIN(m.created_at) AS created_at,
                MAX(m.created_at) AS updated_at,
                SUBSTR((SELECT content FROM messages title WHERE title.conversation_id=c.id
                    AND title.role='user' ORDER BY title.id LIMIT 1),1,80) AS title
                FROM conversations c JOIN messages m ON m.conversation_id=c.id
                WHERE c.company_id=? AND c.user_id=? AND c.policy_key=?
                GROUP BY c.id ORDER BY updated_at DESC LIMIT 50''',
                (identity.company_id, identity.user_id, identity.policy_key)).fetchall()
        return [dict(row) for row in rows]

    def artifacts(self, conversation_id: str, identity: Identity) -> list[dict]:
        self.assert_owner(conversation_id, identity)
        with self.connect() as db:
            rows = db.execute('SELECT id AS report_id,filename,format FROM reports WHERE conversation_id=?',
                              (conversation_id,)).fetchall()
        return [{**dict(row), 'download_url': f'/v1/reports/{row["report_id"]}/download'} for row in rows]

    def save_turn(self, conversation_id: str, identity: Identity, message: str, reply: str, reports: list[dict]):
        self.assert_owner(conversation_id, identity)
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            context = getattr(reply, 'query_context', None)
            db.executemany('INSERT INTO messages(conversation_id,role,content,created_at,model_content,query_context) VALUES (?,?,?,?,?,?)',
                           [(conversation_id, 'user', str(message), now, getattr(message, 'model_content', str(message)), None),
                            (conversation_id, 'assistant', str(reply), now, getattr(reply, 'model_content', str(reply)),
                             json.dumps(context) if context is not None else None)])
            for report in reports:
                db.execute('INSERT INTO reports VALUES (?,?,?,?,?,?)', (
                    report['report_id'], conversation_id, report['filename'], report['format'],
                    report['payload'], json.dumps(report['employee_ids']),
                ))

    def get_report(self, report_id: str, identity: Identity) -> dict:
        with self.connect() as db:
            row = db.execute('''SELECT r.* FROM reports r JOIN conversations c ON c.id=r.conversation_id
                WHERE r.id=? AND c.company_id=? AND c.user_id=?''',
                (report_id, identity.company_id, identity.user_id)).fetchone()
        if row is None:
            raise HTTPException(404, 'Report not found.')
        self.assert_owner(row['conversation_id'], identity)
        return dict(row)

    def delete(self, conversation_id: str, identity: Identity):
        self.assert_owner(conversation_id, identity)
        with self.connect() as db:
            db.execute('DELETE FROM conversations WHERE id=?', (conversation_id,))
