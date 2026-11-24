"""Authorized read models for orders, inventory, coordination and audit."""

from .access import Authorizer
from .booking import booking_row
from .catalog import REPAIR_COUNT
from .domain import BusinessError, Role, day_bounds

ORDER_SELECT = """
    SELECT b.*,h.name AS hotel,t.name AS type_name,t.max_guests,h.photo,
      EXISTS(SELECT 1 FROM coordination c WHERE c.booking_id=b.id AND c.resolution IS NULL)
      AS coordinating
    FROM bookings b JOIN hotels h ON h.id=b.hotel_id JOIN room_types t ON t.id=b.room_type
"""


class ReportingService:
    def __init__(self, database):
        self.database = database

    def hotels(self, actor):
        with self.database.read() as db:
            if actor.role == Role.STAFF:
                return db.execute(
                    """SELECT h.* FROM hotels h JOIN grants g ON g.hotel_id=h.id
                                     WHERE g.user_id=? ORDER BY h.name""",
                    (actor.id,),
                ).fetchall()
            return db.execute("SELECT * FROM hotels ORDER BY name").fetchall()

    def room_types(self, actor, hotel_id):
        with self.database.read() as db:
            Authorizer.hotel(db, actor, hotel_id, admin=True)
            return db.execute(
                "SELECT * FROM room_types WHERE hotel_id=? ORDER BY name", (hotel_id,)
            ).fetchall()

    def rooms(self, actor, hotel_id):
        with self.database.read() as db:
            Authorizer.hotel(db, actor, hotel_id, admin=True)
            return db.execute(
                """SELECT r.*,t.name AS type_name,p.reason,p.last_night FROM rooms r
                   JOIN room_types t ON t.id=r.room_type LEFT JOIN repairs p
                   ON p.room_id=r.id AND p.ended IS NULL WHERE r.hotel_id=? ORDER BY r.number""",
                (hotel_id,),
            ).fetchall()

    def stock(self, actor, hotel_id, first):
        with self.database.read() as db:
            Authorizer.hotel(db, actor, hotel_id, admin=True)
            return db.execute(
                f"""SELECT i.*,t.name,{REPAIR_COUNT} AS repairs FROM inventory i
                    JOIN room_types t ON t.id=i.room_type WHERE t.hotel_id=? AND i.night>=?
                    ORDER BY i.night,t.name LIMIT 60""",
                (hotel_id, first),
            ).fetchall()

    def detail(self, actor, booking_id):
        with self.database.read() as db:
            row = booking_row(db, booking_id)
            Authorizer.booking(db, actor, row)
            detail = db.execute(ORDER_SELECT + " WHERE b.id=?", (booking_id,)).fetchone()
            revisions = db.execute(
                """SELECT r.*,t.name AS type_name FROM revisions r
                   JOIN room_types t ON t.id=r.room_type WHERE booking_id=? ORDER BY version DESC""",
                (booking_id,),
            ).fetchall()
            nights = db.execute(
                "SELECT * FROM nightly_prices WHERE booking_id=? ORDER BY version DESC,night",
                (booking_id,),
            ).fetchall()
            rooms = db.execute(
                """SELECT a.*,r.number FROM assignments a JOIN rooms r ON r.id=a.room_id
                   WHERE a.booking_id=? ORDER BY a.started DESC""",
                (booking_id,),
            ).fetchall()
            return detail, revisions, nights, rooms

    @staticmethod
    def selection(db, actor, hotel, start, end, state=""):
        clauses = ["1=1"]
        args = []
        if actor.role == Role.CUSTOMER:
            clauses.append("b.customer=?")
            args.append(actor.id)
        if actor.role == Role.STAFF:
            clauses.append("EXISTS (SELECT 1 FROM grants WHERE user_id=? AND hotel_id=b.hotel_id)")
            args.append(actor.id)
        if hotel:
            if actor.role != Role.CUSTOMER:
                Authorizer.hotel(db, actor, hotel, admin=True)
            clauses.append("b.hotel_id=?")
            args.append(hotel)
        if start or end:
            if not start or not end:
                raise BusinessError("请同时填写创建日起止日期.", 400)
            clauses.append("b.created>=? AND b.created<?")
            args.extend(day_bounds(start, end))
        if state:
            clauses.append("b.state=?")
            args.append(state)
        return " WHERE " + " AND ".join(clauses), args

    def orders(self, actor, hotel=None, start="", end="", state=""):
        with self.database.read() as db:
            criteria, args = self.selection(db, actor, hotel, start, end, state)
            return db.execute(
                ORDER_SELECT + criteria + " ORDER BY b.id DESC LIMIT 150", args
            ).fetchall()

    def summary(self, actor, hotel=None, start="", end=""):
        Authorizer.role(actor, Role.ADMIN, Role.STAFF)
        with self.database.read() as db:
            criteria, args = self.selection(db, actor, hotel, start, end)
            totals = db.execute(
                """SELECT COUNT(*) AS count, COUNT(CASE WHEN state='已取消' THEN 1 END) AS cancelled,
                   COALESCE(SUM(CASE WHEN state='已完成' THEN total ELSE 0 END),0) AS revenue
                   FROM bookings b"""
                + criteria,
                args,
            ).fetchone()
            breakdown = db.execute(
                "SELECT state,COUNT(*) AS count FROM bookings b" + criteria + " GROUP BY state",
                args,
            ).fetchall()
            return totals, breakdown

    def audit(self, actor, target="", outcome="", start="", end="", action=""):
        Authorizer.role(actor, Role.ADMIN)
        criteria, args = ["1=1"], []
        for field, value in [("target", target), ("outcome", outcome), ("action", action)]:
            if value:
                criteria.append(f"instr({field},?)>0")
                args.append(value)
        if start or end:
            if not start or not end:
                raise BusinessError("请同时填写日期起点和终点.", 400)
            criteria.append("occurred>=? AND occurred<?")
            args.extend(day_bounds(start, end))
        with self.database.read() as db:
            return db.execute(
                "SELECT * FROM audit WHERE "
                + " AND ".join(criteria)
                + " ORDER BY occurred DESC,rowid DESC LIMIT 100",
                args,
            ).fetchall()

    def coordination(self, actor, hotel=None):
        Authorizer.role(actor, Role.STAFF, Role.ADMIN)
        with self.database.read() as db:
            criteria, args = self.selection(db, actor, hotel, "", "")
            return db.execute(
                """SELECT c.*,b.state,h.name AS hotel FROM coordination c
                   JOIN bookings b ON b.id=c.booking_id JOIN hotels h ON h.id=b.hotel_id"""
                + criteria
                + " ORDER BY c.resolution IS NOT NULL,c.id DESC LIMIT 100",
                args,
            ).fetchall()

    def staff_grants(self, actor):
        Authorizer.role(actor, Role.ADMIN)
        with self.database.read() as db:
            return db.execute(
                """SELECT u.id,u.name,u.username,h.name AS hotel,h.id AS hotel_id FROM users u
                   LEFT JOIN grants g ON g.user_id=u.id LEFT JOIN hotels h ON h.id=g.hotel_id
                   WHERE u.role='staff' ORDER BY u.id,h.name""",
            ).fetchall()
