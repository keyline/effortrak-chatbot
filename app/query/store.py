"""Shared service metadata storage; never connects to application business tables.

SQLite is the local default. PostgreSQL supports multiple hosts using the same
tables, expiring leases and private report blobs. SQLAlchemy binds every value.
"""

import json
import time
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    and_,
    create_engine,
    delete,
    event,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError


class Store:
    def __init__(self, path, database_url="", retention_days=30):
        path.parent.mkdir(parents=True, exist_ok=True)
        url = database_url or "sqlite:///" + path.as_posix()
        options = {"pool_pre_ping": True}
        if url.startswith("sqlite:"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 5}
        else:
            options.update(pool_size=5, max_overflow=5, pool_timeout=5)
        self.engine = create_engine(url, **options)
        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "connect")
            def configure(connection, record):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")

        metadata = MetaData()
        self.conversations_table = Table(
            "nlq_conversations",
            metadata,
            Column("id", String(36), primary_key=True),
            Column("company_id", Integer, nullable=False),
            Column("user_id", Integer, nullable=False),
            Column("policy_key", Text, nullable=False),
            Column("title", String(80), nullable=False, default=""),
            Column("created_at", Float, nullable=False),
            Column("updated_at", Float, nullable=False),
        )
        self.messages = Table(
            "nlq_messages",
            metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column(
                "conversation_id",
                String(36),
                ForeignKey("nlq_conversations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            Column("role", String(12), nullable=False),
            Column("content", Text, nullable=False),
            Column("created_at", String(40), nullable=False),
            Column("query_context", Text),
            Column("charts", Text),
            Column("tables", Text),
            Column("status", String(20), nullable=False),
        )
        self.reports = Table(
            "nlq_reports",
            metadata,
            Column("id", String(36), primary_key=True),
            Column(
                "conversation_id",
                String(36),
                ForeignKey("nlq_conversations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            Column("filename", String(200), nullable=False),
            Column("format", String(8), nullable=False),
            Column("payload", LargeBinary, nullable=False),
            Column("export_snapshot", Text, nullable=False),
            Column("expires_at", Float, nullable=False),
        )
        self.leases = Table(
            "nlq_leases",
            metadata,
            Column("resource", String(160), primary_key=True),
            Column("token", String(36), nullable=False),
            Column("expires_at", Float, nullable=False),
        )
        self.jobs = Table(
            "nlq_export_jobs",
            metadata,
            Column("id", String(36), primary_key=True),
            Column(
                "conversation_id",
                String(36),
                ForeignKey("nlq_conversations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            Column("state", String(16), nullable=False),
            Column("payload", Text, nullable=False),
            Column("token", String(36)),
            Column("lease_until", Float, nullable=False, default=0),
            Column("created_at", Float, nullable=False),
            Column("expires_at", Float, nullable=False),
            Column("attempts", Integer, nullable=False, default=0),
            Column("artifacts", Text),
        )
        Index(
            "nlq_conversation_owner",
            self.conversations_table.c.company_id,
            self.conversations_table.c.user_id,
            self.conversations_table.c.updated_at,
        )
        Index(
            "nlq_message_conversation",
            self.messages.c.conversation_id,
            self.messages.c.id,
        )
        Index("nlq_report_expiry", self.reports.c.expires_at)
        Index("nlq_report_conversation", self.reports.c.conversation_id)
        Index("nlq_job_conversation", self.jobs.c.conversation_id)
        Index("nlq_export_queue", self.jobs.c.state, self.jobs.c.created_at)
        Index("nlq_lease_expiry", self.leases.c.expires_at)
        metadata.create_all(self.engine)
        self.prune(retention_days)

    def close(self):
        self.engine.dispose()

    def prune(self, retention_days):
        with self.engine.begin() as db:
            now = time.time()
            cutoff = now - retention_days * 86400
            stale_jobs = (
                (self.jobs.c.state == "working")
                & (self.jobs.c.attempts >= 3)
                & (self.jobs.c.lease_until <= now)
            )
            payloads = (
                db.execute(
                    select(self.jobs.c.payload)
                    .join(
                        self.conversations_table,
                        self.conversations_table.c.id == self.jobs.c.conversation_id,
                    )
                    .where(
                        (self.conversations_table.c.updated_at < cutoff) | stale_jobs
                    )
                )
                .scalars()
                .all()
            )
            for payload in payloads:
                self._release_queue(db, payload)
            db.execute(delete(self.reports).where(self.reports.c.expires_at <= now))
            db.execute(delete(self.jobs).where(self.jobs.c.expires_at <= now))
            db.execute(
                delete(self.conversations_table).where(
                    self.conversations_table.c.updated_at < cutoff
                )
            )
            db.execute(delete(self.leases).where(self.leases.c.expires_at < now - 300))
            db.execute(
                update(self.jobs)
                .where(stale_jobs)
                .values(state="failed", payload="", artifacts="[]")
            )

    def _release_queue(self, db, payload):
        pending = (
            json.loads(payload)
            if isinstance(payload, str) and payload
            else payload or {}
        )
        queue_lease = pending.get("queue_lease")
        if queue_lease:
            db.execute(
                delete(self.leases).where(
                    self.leases.c.resource == queue_lease[0],
                    self.leases.c.token == queue_lease[1],
                )
            )

    def owner(self, db, conversation_id, identity):
        c = self.conversations_table.c
        row = (
            db.execute(
                select(self.conversations_table).where(
                    c.id == conversation_id,
                    c.company_id == identity.company_id,
                    c.user_id == identity.user_id,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(404, "Conversation not found.")
        if row["policy_key"] != identity.policy_key:
            raise HTTPException(409, "Permissions changed. Start a new conversation.")
        return row

    def create(self, identity):
        identifier = str(uuid4())
        with self.engine.begin() as db:
            db.execute(
                insert(self.conversations_table).values(
                    id=identifier,
                    company_id=identity.company_id,
                    user_id=identity.user_id,
                    policy_key=identity.policy_key,
                    title="",
                    created_at=time.time(),
                    updated_at=time.time(),
                )
            )
        return identifier

    def history(self, conversation_id, identity):
        with self.engine.connect() as db:
            self.owner(db, conversation_id, identity)
            rows = (
                db.execute(
                    select(self.messages)
                    .where(self.messages.c.conversation_id == conversation_id)
                    .order_by(self.messages.c.id.desc())
                    .limit(200)
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in reversed(rows)]

    def conversations(self, identity):
        c = self.conversations_table.c
        with self.engine.connect() as db:
            rows = (
                db.execute(
                    select(
                        c.id.label("conversation_id"),
                        c.title,
                        c.created_at,
                        c.updated_at,
                    )
                    .where(
                        c.company_id == identity.company_id,
                        c.user_id == identity.user_id,
                        c.policy_key == identity.policy_key,
                        c.title != "",
                    )
                    .order_by(c.updated_at.desc())
                    .limit(50)
                )
                .mappings()
                .all()
            )
        return [
            {
                **row,
                "created_at": datetime.fromtimestamp(
                    row["created_at"], timezone.utc
                ).isoformat(),
                "updated_at": datetime.fromtimestamp(
                    row["updated_at"], timezone.utc
                ).isoformat(),
            }
            for row in rows
        ]

    def save_turn(self, conversation_id, identity, message, reply, reports, lease):
        job = getattr(reply, "export_job", None)
        queue_lease = None
        if job:
            for slot in range(8):
                try:
                    queue_lease = self.claim(
                        f"export-queue:{identity.company_id}:{slot}",
                        max(1, job["receipt"]["expires_at"] - time.time()),
                    )
                    break
                except HTTPException:
                    continue
            if queue_lease is None:
                raise HTTPException(
                    429, "Your company has several exports queued. Please retry later."
                )
            job["queue_lease"] = queue_lease
        try:
            self._save_turn(conversation_id, identity, message, reply, reports, lease)
        except BaseException:
            if queue_lease:
                self.release(queue_lease)
            raise

    def _save_turn(self, conversation_id, identity, message, reply, reports, lease):
        now = datetime.now(timezone.utc).isoformat()
        with self.engine.begin() as db:
            row = self.owner(db, conversation_id, identity)
            if not db.execute(
                select(self.leases.c.token).where(
                    self.leases.c.resource == lease[0],
                    self.leases.c.token == lease[1],
                    self.leases.c.expires_at > time.time(),
                )
            ).first():
                raise HTTPException(409, "This chat turn expired. Please retry.")
            for role, content in [("user", message), ("assistant", reply)]:
                db.execute(
                    insert(self.messages).values(
                        conversation_id=conversation_id,
                        role=role,
                        content=str(content),
                        created_at=now,
                        query_context=json.dumps(
                            getattr(content, "query_context", None)
                        ),
                        charts=json.dumps(getattr(content, "charts", [])),
                        tables=json.dumps(getattr(content, "tables", [])),
                        status=getattr(content, "status", "answered"),
                    )
                )
            db.execute(
                update(self.conversations_table)
                .where(self.conversations_table.c.id == conversation_id)
                .values(title=row["title"] or str(message)[:80], updated_at=time.time())
            )
            for report in reports:
                receipt = report["export_snapshot"]
                db.execute(
                    insert(self.reports).values(
                        id=report["report_id"],
                        conversation_id=conversation_id,
                        filename=report["filename"],
                        format=report["format"],
                        payload=report["payload"],
                        export_snapshot=json.dumps(receipt),
                        expires_at=receipt["expires_at"],
                    )
                )
            job = getattr(reply, "export_job", None)
            if job:
                db.execute(
                    insert(self.jobs).values(
                        id=job["job_id"],
                        conversation_id=conversation_id,
                        state="queued",
                        payload=json.dumps(job),
                        lease_until=0,
                        attempts=0,
                        created_at=time.time(),
                        expires_at=job["receipt"]["expires_at"],
                    )
                )

    def next_job(self):
        now, token = time.time(), str(uuid4())
        eligible = and_(
            self.jobs.c.expires_at > now,
            self.jobs.c.attempts < 3,
            (self.jobs.c.state == "queued")
            | ((self.jobs.c.state == "working") & (self.jobs.c.lease_until <= now)),
        )
        with self.engine.begin() as db:
            row = (
                db.execute(
                    select(self.jobs)
                    .where(eligible)
                    .order_by(self.jobs.c.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            changed = db.execute(
                update(self.jobs)
                .where(self.jobs.c.id == row["id"], eligible)
                .values(
                    state="working",
                    token=token,
                    lease_until=now + 180,
                    attempts=self.jobs.c.attempts + 1,
                )
            ).rowcount
            if not changed:
                return None
        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "token": token,
            "payload": json.loads(row["payload"]),
        }

    def finish_job(self, job, reports=None):
        with self.engine.begin() as db:
            current = db.execute(
                select(self.jobs.c.id)
                .where(
                    self.jobs.c.id == job["id"],
                    self.jobs.c.token == job["token"],
                    self.jobs.c.state == "working",
                    self.jobs.c.lease_until > time.time(),
                    self.jobs.c.expires_at > time.time(),
                )
                .with_for_update()
            ).first()
            if current is None:
                return
            if reports is not None:
                for report in reports:
                    db.execute(
                        insert(self.reports).values(
                            id=report["report_id"],
                            conversation_id=job["conversation_id"],
                            filename=report["filename"],
                            format=report["format"],
                            payload=report["payload"],
                            export_snapshot=json.dumps(report["export_snapshot"]),
                            expires_at=report["export_snapshot"]["expires_at"],
                        )
                    )
            artifacts = [
                {
                    key: report[key]
                    for key in ("report_id", "filename", "format", "download_url")
                }
                for report in reports or []
            ]
            db.execute(
                update(self.jobs)
                .where(self.jobs.c.id == job["id"], self.jobs.c.token == job["token"])
                .values(
                    state="ready" if reports is not None else "failed",
                    payload="",
                    artifacts=json.dumps(artifacts),
                    lease_until=0,
                )
            )
            queue_lease = job["payload"].get("queue_lease")
            if queue_lease:
                db.execute(
                    delete(self.leases).where(
                        self.leases.c.resource == queue_lease[0],
                        self.leases.c.token == queue_lease[1],
                    )
                )

    def job(self, identifier, identity):
        with self.engine.connect() as db:
            c, j = self.conversations_table.c, self.jobs.c
            row = (
                db.execute(
                    select(self.jobs)
                    .join(self.conversations_table, c.id == j.conversation_id)
                    .where(
                        j.id == identifier,
                        c.company_id == identity.company_id,
                        c.user_id == identity.user_id,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise HTTPException(404, "Report job not found.")
            self.owner(db, row["conversation_id"], identity)
            if row["expires_at"] <= time.time():
                raise HTTPException(410, "This report expired. Generate it again.")
            if not identity.can_export:
                raise HTTPException(
                    403, "You no longer have permission to export reports."
                )
            return {
                "job_id": row["id"],
                "status": row["state"],
                "artifacts": json.loads(row["artifacts"] or "[]"),
            }

    def conversation_jobs(self, conversation_id, identity):
        with self.engine.connect() as db:
            self.owner(db, conversation_id, identity)
            rows = (
                db.execute(
                    select(self.jobs.c.id, self.jobs.c.state)
                    .where(
                        self.jobs.c.conversation_id == conversation_id,
                        self.jobs.c.expires_at > time.time(),
                    )
                    .order_by(self.jobs.c.created_at)
                    .limit(100)
                )
                .mappings()
                .all()
            )
        return [{"job_id": row["id"], "status": row["state"]} for row in rows]

    def artifacts(self, conversation_id, identity):
        with self.engine.connect() as db:
            self.owner(db, conversation_id, identity)
            rows = (
                db.execute(
                    select(
                        self.reports.c.id,
                        self.reports.c.filename,
                        self.reports.c.format,
                    ).where(
                        self.reports.c.conversation_id == conversation_id,
                        self.reports.c.expires_at > time.time(),
                    )
                )
                .mappings()
                .all()
            )
        return [
            {
                "report_id": row["id"],
                "filename": row["filename"],
                "format": row["format"],
                "download_url": f"/v1/reports/{row['id']}/download",
            }
            for row in rows
        ]

    def get_report(self, identifier, identity):
        with self.engine.connect() as db:
            c, r = self.conversations_table.c, self.reports.c
            row = (
                db.execute(
                    select(self.reports)
                    .join(self.conversations_table, c.id == r.conversation_id)
                    .where(
                        r.id == identifier,
                        c.company_id == identity.company_id,
                        c.user_id == identity.user_id,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise HTTPException(404, "Report not found.")
            self.owner(db, row["conversation_id"], identity)
            if row["expires_at"] <= time.time():
                raise HTTPException(410, "This report expired. Generate it again.")
        return dict(row)

    def delete(self, conversation_id, identity):
        with self.engine.begin() as db:
            self.owner(db, conversation_id, identity)
            for payload in db.execute(
                select(self.jobs.c.payload).where(
                    self.jobs.c.conversation_id == conversation_id
                )
            ).scalars():
                pending = json.loads(payload) if payload else {}
                queue_lease = pending.get("queue_lease")
                if queue_lease:
                    db.execute(
                        delete(self.leases).where(
                            self.leases.c.resource == queue_lease[0],
                            self.leases.c.token == queue_lease[1],
                        )
                    )
            db.execute(
                delete(self.conversations_table).where(
                    self.conversations_table.c.id == conversation_id
                )
            )

    def claim(self, resource, ttl=120):
        token, now = str(uuid4()), time.time()
        try:
            with self.engine.begin() as db:
                changed = db.execute(
                    update(self.leases)
                    .where(
                        self.leases.c.resource == resource,
                        self.leases.c.expires_at <= now,
                    )
                    .values(token=token, expires_at=now + ttl)
                ).rowcount
                if not changed:
                    db.execute(
                        insert(self.leases).values(
                            resource=resource, token=token, expires_at=now + ttl
                        )
                    )
        except IntegrityError:
            raise HTTPException(
                409, "A request is already running. Please wait and retry."
            ) from None
        return resource, token

    def release(self, lease):
        with self.engine.begin() as db:
            db.execute(
                delete(self.leases).where(
                    self.leases.c.resource == lease[0], self.leases.c.token == lease[1]
                )
            )
