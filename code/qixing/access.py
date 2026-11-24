"""Identity verification and object-level authorization."""

import hashlib
import hmac
import secrets
import sqlite3

from .domain import Actor, BusinessError, Role
from .storage import one

ITERATIONS = 240_000


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return f"{salt}:{digest.hex()}"


class Authorizer:
    def __init__(self, database):
        self.database = database

    def login(self, username: str, password: str) -> Actor:
        with self.database.read() as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        salt = row["password"].split(":")[0] if row else "0" * 32
        expected = row["password"] if row else f"{salt}:{'0' * 64}"
        valid = hmac.compare_digest(hash_password(password, salt), expected)
        if not valid or row is None:
            raise BusinessError("账号或密码不正确.", 401)
        return Actor(row["id"], row["name"], Role(row["role"]))

    def actor(self, user_id: int) -> Actor:
        with self.database.read() as db:
            row = one(db, "SELECT id,name,role FROM users WHERE id=?", (user_id,))
        return Actor(row["id"], row["name"], Role(row["role"]))

    @staticmethod
    def role(actor: Actor, *allowed: Role):
        if actor.role not in allowed:
            raise BusinessError("当前角色无权执行此操作.", 403)

    @classmethod
    def hotel(cls, db: sqlite3.Connection, actor, hotel, *, admin=False):
        if admin and actor.role == Role.ADMIN:
            return
        cls.role(actor, Role.STAFF)
        grant = db.execute(
            "SELECT 1 FROM grants WHERE user_id=? AND hotel_id=?",
            (actor.id, hotel),
        ).fetchone()
        if not grant:
            raise BusinessError("无权操作该酒店.", 403)

    @classmethod
    def booking(cls, db, actor, row):
        if actor.role == Role.CUSTOMER:
            if row["customer"] != actor.id:
                raise BusinessError("订单不存在.", 404)
        else:
            cls.hotel(db, actor, row["hotel_id"], admin=True)
