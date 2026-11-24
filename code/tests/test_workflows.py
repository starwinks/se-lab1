"""Integration checks for the Lab 2 business rules on a real SQLite database."""

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from threading import Barrier

from qixing.access import Authorizer
from qixing.booking import BookingService
from qixing.catalog import CatalogQuery
from qixing.domain import (
    CONFIRM_WINDOW,
    BookingState,
    BusinessError,
    RequestState,
    RoomState,
    Stay,
    day_bounds,
)
from qixing.operations import AuthorizationService, FrontDeskService, HotelService
from qixing.reporting import ReportingService
from qixing.seed import seed
from qixing.storage import Database

DAY = date(2026, 11, 24)
BASE = day_bounds(DAY.isoformat(), DAY.isoformat())[0]


class Workflows(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.sqlite"
        self.instant = BASE
        self.database = Database(self.path, lambda: self.instant)
        self.database.initialize()
        seed(self.database, DAY)
        auth = Authorizer(self.database)
        self.customer, self.other, self.staff, self.admin, self.foreign = (
            auth.actor(i) for i in range(1, 6)
        )
        self.booking = BookingService(self.database)
        self.catalog = CatalogQuery(self.database)
        self.desk = FrontDeskService(self.database)
        self.hotels = HotelService(self.database)
        self.reporting = ReportingService(self.database)

    @staticmethod
    def stay(start=0, end=2, room_type=1, guest="林玥"):
        return Stay(room_type, DAY + timedelta(days=start), DAY + timedelta(days=end), 1, guest)

    def reserve(self, key="initial", stay=None):
        return self.booking.submit(self.customer, key, stay or self.stay()).booking_id

    def order(self, booking_id):
        return self.reporting.detail(self.customer, booking_id)[0]

    def quantities(self, room_type=1, days=4):
        with self.database.read() as db:
            return [
                r["occupied"]
                for r in db.execute(
                    "SELECT occupied FROM inventory WHERE room_type=? ORDER BY night LIMIT ?",
                    (room_type, days),
                )
            ]

    def test_price_snapshot_guest_only_and_overlap(self):
        ident = self.reserve()
        original = self.order(ident)
        self.booking.confirm(self.staff, ident)
        self.hotels.save_inventory(self.staff, 1, DAY.isoformat(), 3, 70_000, False)
        self.instant += 500
        self.booking.modify(self.customer, ident, self.stay(guest="陈玥"))
        updated = self.order(ident)
        self.assertEqual(updated["total"], original["total"])
        self.assertEqual((updated["state"], updated["version"]), (BookingState.PENDING, 2))
        self.assertEqual(updated["waiting_since"], self.instant)
        self.assertEqual(updated["created"], original["created"])
        self.assertEqual(self.quantities(), [1, 1, 0, 0])
        self.booking.modify(self.customer, ident, self.stay(1, 4))
        self.assertEqual(self.quantities(), [0, 1, 1, 1])
        _, revisions, prices, _ = self.reporting.detail(self.customer, ident)
        self.assertEqual([r["version"] for r in revisions], [3, 2, 1])
        self.assertEqual(len(prices), 7)

    def test_changed_type_releases_previous_allocation(self):
        ident = self.reserve()
        self.booking.modify(self.customer, ident, self.stay(room_type=2))
        self.assertEqual(self.quantities(1), [0, 0, 0, 0])
        self.assertEqual(self.quantities(2), [1, 1, 0, 0])
        self.assertEqual(self.order(ident)["room_type"], 2)
        with self.assertRaises(BusinessError):
            self.booking.modify(self.customer, ident, self.stay(room_type=3))
        self.assertEqual(self.quantities(2), [1, 1, 0, 0])

    def test_missing_night_and_sold_out_roll_back(self):
        ident = self.reserve()
        self.hotels.save_inventory(
            self.staff, 1, (DAY + timedelta(days=2)).isoformat(), 0, 60_000, False
        )
        with self.assertRaisesRegex(BusinessError, "余量不足"):
            self.booking.modify(self.customer, ident, self.stay(1, 3))
        self.assertEqual(self.quantities(), [1, 1, 0, 0])
        self.assertEqual(self.order(ident)["version"], 1)
        self.assertEqual(self.order(ident)["waiting_since"], self.instant)
        with self.assertRaisesRegex(BusinessError, "尚未开放"):
            self.reserve("missing", self.stay(89, 91))

    def test_confirm_boundary_and_stale_version(self):
        ident = self.reserve()
        self.instant += CONFIRM_WINDOW
        self.booking.confirm(self.staff, ident)
        self.assertEqual(self.order(ident)["state"], BookingState.CONFIRMED)
        self.booking.modify(self.customer, ident, self.stay(guest="新入住人"))
        self.instant += CONFIRM_WINDOW
        self.assertFalse(self.booking.expire(ident, 1))
        self.assertFalse(self.booking.expire(ident, 2))
        self.instant += 1
        self.assertEqual(self.booking.run_expiry(), 1)
        self.assertEqual(self.booking.run_expiry(), 0)
        self.assertEqual(self.order(ident)["state"], BookingState.CANCELLED)
        self.assertEqual(self.quantities(), [0, 0, 0, 0])

    def test_expired_foreground_command_persists_cancellation(self):
        ident = self.reserve()
        self.instant += CONFIRM_WINDOW + 1
        with self.assertRaisesRegex(BusinessError, "已超时"):
            self.booking.confirm(self.staff, ident)
        self.assertEqual(self.order(ident)["state"], BookingState.CANCELLED)
        self.assertEqual(self.quantities(), [0, 0, 0, 0])

    def test_request_replay_and_sticky_reminder_after_matched_order_changes(self):
        ident = self.reserve()
        self.assertEqual(self.reserve(), ident)
        with self.assertRaisesRegex(BusinessError, "另一组"):
            self.reserve("initial", self.stay(0, 3))
        waiting = self.booking.submit(self.customer, "second", self.stay())
        self.assertEqual(waiting.state, RequestState.WAITING)
        self.booking.modify(self.customer, ident, self.stay(1, 3))
        self.assertEqual(self.booking.submit(self.customer, "second", self.stay()), waiting)
        with self.assertRaisesRegex(BusinessError, "提醒已失效"):
            self.booking.submit(self.customer, "second", self.stay(), "continue", "invalid")
        continued = self.booking.submit(
            self.customer, "second", self.stay(), "continue", waiting.challenge
        )
        self.assertNotEqual(continued.booking_id, ident)
        self.assertEqual(self.reserve("second"), continued.booking_id)
        self.assertEqual(self.quantities(), [1, 2, 1, 0])

    def test_decline_replay_does_not_occupy_inventory(self):
        self.reserve()
        waiting = self.booking.submit(self.customer, "second", self.stay())
        result = self.booking.submit(
            self.customer, "second", self.stay(), "decline", waiting.challenge
        )
        self.assertEqual(result.state, RequestState.DECLINED)
        self.assertEqual(
            self.booking.submit(self.customer, "second", self.stay()).state, RequestState.DECLINED
        )
        self.assertEqual(self.quantities(), [1, 1, 0, 0])

    def test_stale_query_after_stop(self):
        self.assertTrue(self.catalog.search(DAY, DAY + timedelta(days=2), city="南京"))
        self.hotels.save_inventory(self.staff, 1, DAY.isoformat(), 3, 30_000, True)
        with self.assertRaisesRegex(BusinessError, "停售"):
            self.reserve()
        self.assertEqual(self.quantities(), [0, 0, 0, 0])

    def test_twenty_concurrent_customers_for_one_room(self):
        with self.database.transaction() as db:
            db.execute("UPDATE inventory SET total=1 WHERE room_type=1")
            db.executemany(
                "INSERT INTO users(id,username,name,password,role) VALUES(?,?,?,?,?)",
                [(i, f"race{i}", f"测试顾客{i}", "unused", "customer") for i in range(10, 30)],
            )
        auth = Authorizer(self.database)
        participants = [auth.actor(i) for i in range(10, 30)]
        ready = Barrier(len(participants))

        def submit(actor):
            ready.wait(timeout=10)
            try:
                return self.booking.submit(actor, "same-local-key", self.stay()).booking_id
            except BusinessError as error:
                self.assertIn("余量不足", str(error))
                return None

        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(submit, participants))
        self.assertEqual(sum(val is not None for val in results), 1)
        self.assertEqual(self.quantities(), [1, 1, 0, 0])

    def test_two_front_desk_requests_cannot_assign_same_room(self):
        requests = [
            self.booking.submit(actor, "race", self.stay()).booking_id
            for actor in (self.customer, self.other)
        ]
        for ident in requests:
            self.booking.confirm(self.staff, ident)
        ready = Barrier(2)

        def check_in(ident):
            ready.wait(timeout=10)
            try:
                self.desk.check_in(self.staff, ident, 1)
                return True
            except BusinessError as error:
                self.assertIn("已不可分配", str(error))
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(check_in, requests)), 1)
        with self.database.read() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM assignments WHERE ended IS NULL").fetchone()[0], 1
            )
        self.assertEqual(self.quantities(), [2, 2, 0, 0])

    def test_concurrent_replay_same_customer_and_key(self):
        ready = Barrier(8)

        def send(_):
            ready.wait(timeout=10)
            return self.reserve("shared")

        with ThreadPoolExecutor(max_workers=8) as pool:
            identifiers = list(pool.map(send, range(8)))
        self.assertEqual(len(set(identifiers)), 1)
        self.assertEqual(identifiers[0], 1)
        self.assertEqual(self.quantities(), [1, 1, 0, 0])

    def test_cancel_and_check_in_compete_over_order_state(self):
        ident = self.reserve()
        self.booking.confirm(self.staff, ident)
        ready = Barrier(2)

        def compete(action):
            ready.wait(timeout=10)
            try:
                action()
            except BusinessError:
                return 0
            return 1

        with ThreadPoolExecutor(max_workers=2) as pool:
            successes = list(
                pool.map(
                    compete,
                    [
                        lambda: self.booking.cancel(self.customer, ident),
                        lambda: self.desk.check_in(self.staff, ident, 1),
                    ],
                )
            )
        self.assertEqual(sum(successes), 1)
        if successes[0]:
            self.assertEqual(self.order(ident)["state"], BookingState.CANCELLED)
            self.assertEqual(self.quantities(), [0, 0, 0, 0])
        else:
            self.assertEqual(self.order(ident)["state"], BookingState.IN_HOUSE)
            self.assertEqual(self.quantities(), [1, 1, 0, 0])

    def test_permissions_and_checkout_state(self):
        ident = self.reserve()
        with self.assertRaises(BusinessError):
            self.reporting.detail(self.other, ident)
        with self.assertRaises(BusinessError):
            self.booking.confirm(self.foreign, ident)
        with self.assertRaises(BusinessError):
            self.reporting.audit(self.staff)
        with self.assertRaises(BusinessError):
            AuthorizationService(self.database).set_grant(self.staff, 3, 2, True)
        self.booking.confirm(self.staff, ident)
        room = self.desk.candidates(self.staff, ident)[0]["id"]
        self.desk.check_in(self.staff, ident, room)
        with self.assertRaises(BusinessError):
            self.booking.cancel(self.customer, ident)
        self.desk.check_out(self.staff, ident)
        self.assertEqual(self.order(ident)["state"], BookingState.COMPLETED)
        rows = self.reporting.rooms(self.staff, 1)
        self.assertEqual(next(r for r in rows if r["id"] == room)["state"], RoomState.DIRTY)
        self.desk.clean(self.staff, room)

    def test_repair_and_stop_preserve_old_order(self):
        ident = self.reserve()
        self.hotels.save_inventory(self.staff, 1, DAY.isoformat(), 1, 25_000, False)
        self.desk.repair(self.staff, 1, DAY, DAY, "水管维修")
        self.assertTrue(self.order(ident)["coordinating"])
        self.assertEqual(self.quantities(), [1, 1, 0, 0])
        self.assertFalse(
            any(r["id"] == 1 for r in self.catalog.search(DAY, DAY + timedelta(days=1)))
        )
        self.desk.restore(self.staff, 1)
        self.hotels.close(self.admin, 1, True, "安全检查")
        self.assertTrue(self.order(ident)["coordinating"])
        self.assertEqual(self.order(ident)["state"], BookingState.PENDING)
        with self.assertRaisesRegex(BusinessError, "暂停"):
            self.reserve("late", self.stay(1, 3))

    def test_stats_use_original_created_date_and_saved_amount(self):
        ident = self.reserve()
        self.instant += CONFIRM_WINDOW
        self.booking.confirm(self.staff, ident)
        self.desk.check_in(self.staff, ident, 1)
        self.instant += 24 * 60 * 60 * 1_000_000
        self.desk.check_out(self.staff, ident)
        totals, _ = self.reporting.summary(self.admin, 1, str(DAY), str(DAY))
        self.assertEqual(tuple(totals), (1, 0, self.order(ident)["total"]))
        following = str(DAY + timedelta(days=1))
        totals, _ = self.reporting.summary(self.admin, 1, following, following)
        self.assertEqual(tuple(totals), (0, 0, 0))

    def test_success_audit_failure_rolls_back_and_failure_replays_once(self):
        with self.database.transaction() as db:
            db.execute("""CREATE TRIGGER fail_audit BEFORE INSERT ON audit
                          BEGIN SELECT RAISE(ABORT,'fixture'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.reserve()
        self.assertEqual(self.quantities(), [0, 0, 0, 0])
        recovered = Database(self.path, lambda: self.instant)
        recovered.initialize()
        with recovered.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0], 0)
            db.execute("DROP TRIGGER fail_audit")
        from contextlib import closing

        with closing(recovered.connect(recovered.queue_path)) as queue:
            pending = queue.execute("SELECT * FROM pending").fetchone()
            self.assertIsNotNone(pending)
        self.assertEqual(recovered.replay_failures(), 1)
        # Simulate committing the remote event and then dying before removing the local row.
        with closing(recovered.connect(recovered.queue_path)) as queue:
            queue.execute("INSERT INTO pending VALUES(?,?)", tuple(pending))
        self.assertEqual(recovered.replay_failures(), 1)
        events = ReportingService(recovered).audit(self.admin, outcome="失败")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["operation_id"], json.loads(pending["payload"])[0])

    def test_restart_keeps_order_inventory_and_audit(self):
        ident = self.reserve()
        restored = Database(self.path, lambda: self.instant)
        restored.initialize()
        self.assertEqual(ReportingService(restored).detail(self.customer, ident)[0]["version"], 1)
        with restored.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM allocations").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
