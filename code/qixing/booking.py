"""Booking command service, revision snapshots and strict expiry handling."""

import hmac
import json
import secrets

from .access import Authorizer
from .catalog import CatalogQuery, InventoryService
from .domain import (
    CONFIRM_WINDOW,
    Actor,
    Booking,
    BookingRevision,
    BookingState,
    BusinessError,
    NightPrice,
    RequestState,
    Role,
    Stay,
    Submission,
    date_key,
    required,
)
from .storage import one, snapshot


def booking_row(db, booking_id):
    return one(db, "SELECT * FROM bookings WHERE id=?", (booking_id,), "订单不存在.")


def revision_prices(db, booking_id, version):
    return tuple(
        NightPrice(r["night"], r["price"])
        for r in db.execute(
            "SELECT night,price FROM nightly_prices WHERE booking_id=? AND version=? ORDER BY night",
            (booking_id, version),
        )
    )


def model(db, row):
    stay = Stay(
        row["room_type"],
        date_key(row["arrival"]),
        date_key(row["departure"]),
        row["guests"],
        row["guest"],
    )
    revision = BookingRevision(row["version"], stay, revision_prices(db, row["id"], row["version"]))
    return Booking(
        row["id"],
        row["customer"],
        row["hotel_id"],
        BookingState(row["state"]),
        revision,
        row["waiting_since"],
    )


def save_revision(db, booking_id, revision, instant):
    stay = revision.stay
    db.execute(
        "INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?,?)",
        (
            booking_id,
            revision.version,
            *stay.itinerary,
            stay.guest,
            stay.guests,
            revision.total,
            instant,
        ),
    )
    db.executemany(
        "INSERT INTO nightly_prices VALUES(?,?,?,?)",
        [(booking_id, revision.version, item.night, item.unit_cents) for item in revision.prices],
    )
    db.execute("DELETE FROM timeout_jobs WHERE booking_id=?", (booking_id,))
    db.execute(
        "INSERT INTO timeout_jobs VALUES(?,?,?)",
        (booking_id, revision.version, instant + CONFIRM_WINDOW),
    )


def cancel_booking(db, booking_id, reason):
    InventoryService.apply(db, booking_id, None)
    db.execute(
        "UPDATE bookings SET state=?,cancel_reason=?,cancel_fee=0 WHERE id=?",
        (BookingState.CANCELLED, reason, booking_id),
    )
    db.execute("DELETE FROM timeout_jobs WHERE booking_id=?", (booking_id,))


class BookingService:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def validate_offer(db, stay, expected_hotel=None):
        item = CatalogQuery.room_type(db, stay.room_type)
        if expected_hotel is not None and item["hotel_id"] != expected_hotel:
            raise BusinessError("修改仅支持同酒店房型.")
        if item["closed"]:
            raise BusinessError("该酒店已暂停接受预订.")
        if stay.guests > item["max_guests"]:
            raise BusinessError("人数超过该房型容量.")
        return item

    @staticmethod
    def result(row):
        return Submission(
            RequestState(row["state"]), row["booking_id"], row["challenge"], row["matched_booking"]
        )

    def submit(self, actor: Actor, key: str, stay: Stay, decision="", challenge="") -> Submission:
        with self.database.write(actor, "提交预订", f"请求 {key[:16]}") as (db, change):
            Authorizer.role(actor, Role.CUSTOMER)
            key = required(key, "请求编号", 80)
            parameters = (actor.id, key)
            fingerprint = json.dumps(stay.fingerprint(), ensure_ascii=False)
            db.execute(
                "INSERT OR IGNORE INTO requests VALUES(?,?,?,'OPEN',NULL,NULL,NULL)",
                (*parameters, fingerprint),
            )
            request = one(
                db, "SELECT * FROM requests WHERE customer=? AND request_key=?", parameters
            )
            result = self.result(request)
            if request["fingerprint"] != fingerprint:
                raise BusinessError("同一请求编号不能用于另一组预订参数.")
            if result.state in (RequestState.SUCCEEDED, RequestState.DECLINED):
                change.skip = True
                return result
            if result.state == RequestState.WAITING:
                if not decision:
                    change.skip = True
                    return result
                if decision not in ("continue", "decline") or not hmac.compare_digest(
                    challenge.encode(),
                    result.challenge.encode(),
                ):
                    raise BusinessError("相同行程提醒已失效, 请使用原提醒提交.")
                if decision == "decline":
                    db.execute(
                        "UPDATE requests SET state='DECLINED' WHERE customer=? AND request_key=?",
                        parameters,
                    )
                    change.detail = "顾客放弃相同行程, 未占库存."
                    change.observed({"state": result.state}, {"state": RequestState.DECLINED})
                    return Submission(RequestState.DECLINED)
            elif decision:
                raise BusinessError("当前请求没有待处理的相同行程提醒.")

            item = self.validate_offer(db, stay)
            if result.state == RequestState.OPEN:
                matched = db.execute(
                    """SELECT id FROM bookings WHERE customer=? AND room_type=? AND arrival=?
                       AND departure=? AND state IN ('待确认','已确认','已入住')
                       AND (state!='待确认' OR waiting_since>=?)
                       ORDER BY id DESC LIMIT 1""",
                    (actor.id, *stay.itinerary, self.database.clock() - CONFIRM_WINDOW),
                ).fetchone()
                if matched:
                    token = secrets.token_urlsafe(24)
                    db.execute(
                        """UPDATE requests SET state='AWAITING_CONSENT',challenge=?,
                                  matched_booking=? WHERE customer=? AND request_key=?""",
                        (token, matched["id"], *parameters),
                    )
                    change.detail = "提示相同行程, 尚未创建订单."
                    change.observed(
                        {"state": RequestState.OPEN},
                        {"state": RequestState.WAITING, "matched": matched["id"]},
                    )
                    return Submission(
                        RequestState.WAITING, challenge=token, matched_booking=matched["id"]
                    )
            instant = self.database.clock()
            revision = BookingRevision(1, stay, InventoryService.quote(db, stay))
            cur = db.execute(
                """INSERT INTO bookings(customer,hotel_id,room_type,arrival,departure,guest,
                   guests,total,state,version,created,waiting_since) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    actor.id,
                    item["hotel_id"],
                    *stay.itinerary,
                    stay.guest,
                    stay.guests,
                    revision.total,
                    BookingState.PENDING,
                    1,
                    instant,
                    instant,
                ),
            )
            booking_id = cur.lastrowid
            InventoryService.apply(db, booking_id, stay)
            save_revision(db, booking_id, revision, instant)
            db.execute(
                """UPDATE requests SET state='SUCCEEDED',booking_id=?
                          WHERE customer=? AND request_key=?""",
                (booking_id, *parameters),
            )
            change.target = f"订单 {booking_id}"
            change.after = snapshot(booking_row(db, booking_id))
            return Submission(RequestState.SUCCEEDED, booking_id)

    def expire_if_due(self, db, change, row, instant, version=None):
        order = model(db, row)
        if (version is not None and row["version"] != version) or not order.expired(instant):
            return False
        cancel_booking(db, order.id, "等待确认超过30分钟")
        change.action = "超时取消"
        change.observed(snapshot(row), snapshot(booking_row(db, order.id)))
        return True

    def modify(self, actor, booking_id, stay):
        expired = False
        with self.database.write(actor, "修改预订", f"订单 {booking_id}") as (db, change):
            Authorizer.role(actor, Role.CUSTOMER)
            row = booking_row(db, booking_id)
            Authorizer.booking(db, actor, row)
            change.before = snapshot(row)
            order = model(db, row)
            order.require(BookingState.PENDING, BookingState.CONFIRMED)
            instant = self.database.clock()
            expired = self.expire_if_due(db, change, row, instant)
            if not expired:
                itinerary_changed = order.revision.stay.itinerary != stay.itinerary
                if itinerary_changed:
                    self.validate_offer(db, stay, order.hotel)
                    prices = InventoryService.quote(db, stay)
                    InventoryService.apply(db, booking_id, stay)
                else:
                    item = CatalogQuery.room_type(db, stay.room_type)
                    if stay.guests > item["max_guests"]:
                        raise BusinessError("人数超过该房型容量.")
                    prices = order.revision.prices
                revision = BookingRevision(row["version"] + 1, stay, prices)
                db.execute(
                    """UPDATE bookings SET room_type=?,arrival=?,departure=?,guest=?,
                       guests=?,total=?,state=?,version=?,waiting_since=? WHERE id=?""",
                    (
                        *stay.itinerary,
                        stay.guest,
                        stay.guests,
                        revision.total,
                        BookingState.PENDING,
                        revision.version,
                        instant,
                        booking_id,
                    ),
                )
                save_revision(db, booking_id, revision, instant)
                change.after = snapshot(booking_row(db, booking_id))
        if expired:
            raise BusinessError("该订单已超时并释放库存.")

    def confirm(self, actor, booking_id):
        with self.database.write(actor, "确认预订", f"订单 {booking_id}") as (db, change):
            row = booking_row(db, booking_id)
            Authorizer.hotel(db, actor, row["hotel_id"])
            change.before = snapshot(row)
            model(db, row).require(BookingState.PENDING)
            expired = self.expire_if_due(db, change, row, self.database.clock())
            if not expired:
                db.execute(
                    "UPDATE bookings SET state=? WHERE id=?", (BookingState.CONFIRMED, booking_id)
                )
                db.execute("DELETE FROM timeout_jobs WHERE booking_id=?", (booking_id,))
                change.after = snapshot(booking_row(db, booking_id))
        if expired:
            raise BusinessError("该订单已超时并释放库存.")

    def cancel(self, actor, booking_id):
        with self.database.write(actor, "取消预订", f"订单 {booking_id}") as (db, change):
            Authorizer.role(actor, Role.CUSTOMER)
            row = booking_row(db, booking_id)
            Authorizer.booking(db, actor, row)
            change.before = snapshot(row)
            model(db, row).require(BookingState.PENDING, BookingState.CONFIRMED)
            cancel_booking(db, booking_id, "顾客主动取消")
            change.after = snapshot(booking_row(db, booking_id))

    def expire(self, booking_id, version):
        with self.database.write(None, "超时取消", f"订单 {booking_id}") as (db, change):
            row = booking_row(db, booking_id)
            expired = self.expire_if_due(db, change, row, self.database.clock(), version)
            change.skip = not expired
            return expired

    def run_expiry(self, limit=100):
        with self.database.read() as db:
            jobs = db.execute(
                "SELECT * FROM timeout_jobs WHERE due < ? ORDER BY due LIMIT ?",
                (self.database.clock(), limit),
            ).fetchall()
        return sum(self.expire(row["booking_id"], row["version"]) for row in jobs)
