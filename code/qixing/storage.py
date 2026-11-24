"""Transactions and the independently durable failed-audit queue."""

import json
import logging
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from .domain import Actor, BusinessError, now_us

LOG = logging.getLogger(__name__)
AUDIT_INSERT = """
    INSERT INTO audit(operation_id,occurred,actor,action,target,outcome,detail,
                      before_json,after_json)
    VALUES(?,?,?,?,?,?,?,?,?)
"""


@dataclass
class Change:
    action: str
    target: str
    before: object = None
    after: object = None
    detail: str = ""
    skip: bool = False

    def observed(self, before, after):
        self.before = before
        self.after = after


def snapshot(row):
    return None if row is None else dict(row)


class Database:
    def __init__(self, path: Path, clock=now_us):
        self.path = path
        self.queue_path = path.with_name(path.name + ".audit")
        self.clock = clock

    @staticmethod
    def connect(path: Path) -> sqlite3.Connection:
        db = sqlite3.connect(path, timeout=12, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA synchronous = FULL")
        return db

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect(self.path)) as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(files("qixing").joinpath("schema.sql").read_text(encoding="utf-8"))
        with closing(self.connect(self.queue_path)) as queue:
            queue.execute("PRAGMA journal_mode = WAL")
            queue.execute("CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, payload TEXT)")

    @contextmanager
    def read(self):
        with closing(self.connect(self.path)) as db:
            db.execute("BEGIN")
            yield db

    @contextmanager
    def transaction(self):
        with closing(self.connect(self.path)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def event(self, actor: Actor | None, change: Change, outcome: str, detail: str):
        before = json.dumps(change.before, ensure_ascii=False)
        after = json.dumps(change.after if outcome == "成功" else change.before, ensure_ascii=False)
        return (
            uuid4().hex,
            self.clock(),
            actor.name if actor else "系统",
            change.action,
            change.target,
            outcome,
            detail,
            before,
            after,
        )

    @contextmanager
    def write(self, actor: Actor | None, action: str, target: str):
        change = Change(action, target)
        try:
            with self.transaction() as db:
                yield db, change
                if not change.skip:
                    db.execute(AUDIT_INSERT, self.event(actor, change, "成功", change.detail))
        except (BusinessError, sqlite3.Error) as error:
            detail = (
                str(error) if isinstance(error, BusinessError) else "数据库写入失败, 业务已回滚."
            )
            try:
                self.persist_failure(self.event(actor, change, "失败", detail))
            except sqlite3.Error:
                LOG.exception("业务库和本地失败队列均无法保存审计")
            raise

    def persist_failure(self, record):
        try:
            with self.transaction() as db:
                db.execute(AUDIT_INSERT, record)
        except sqlite3.Error:
            with closing(self.connect(self.queue_path)) as queue:
                queue.execute(
                    "INSERT OR IGNORE INTO pending VALUES (?,?)",
                    (record[0], json.dumps(record, ensure_ascii=False)),
                )

    def replay_failures(self, limit: int = 50) -> int:
        with closing(self.connect(self.queue_path)) as queue:
            pending = queue.execute(
                "SELECT id,payload FROM pending ORDER BY rowid LIMIT ?", (limit,)
            )
            records = pending.fetchall()
        for row in records:
            with self.transaction() as db:
                db.execute(
                    AUDIT_INSERT + " ON CONFLICT(operation_id) DO NOTHING",
                    json.loads(row["payload"]),
                )
            with closing(self.connect(self.queue_path)) as queue:
                queue.execute("DELETE FROM pending WHERE id=?", (row["id"],))
        return len(records)


def one(db, sql, parameters, missing="记录不存在."):
    row = db.execute(sql, parameters).fetchone()
    if row is None:
        raise BusinessError(missing, 404)
    return row
