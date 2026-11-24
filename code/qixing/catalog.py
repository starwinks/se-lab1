"""Catalog search and authoritative nightly stock calculations."""

from collections.abc import Iterable
from datetime import timedelta

from .domain import BusinessError, DailyInventory, NightPrice, Stay
from .storage import one

REPAIR_COUNT = """(
    SELECT COUNT(*) FROM repairs r
    WHERE r.room_type=i.room_type AND r.ended IS NULL
      AND i.night BETWEEN r.first_night AND r.last_night
)"""


class InventoryService:
    @staticmethod
    def days(db, room_type, nights: Iterable[str]) -> dict[str, DailyInventory]:
        nights = tuple(nights)
        if not nights:
            return {}
        rows = db.execute(
            f"""SELECT i.*, {REPAIR_COUNT} AS repairs FROM inventory i
                WHERE i.room_type=? AND i.night IN ({",".join("?" for _ in nights)})""",
            (room_type, *nights),
        )
        return {
            r["night"]: DailyInventory(
                r["total"], r["occupied"], r["repairs"], r["price"], bool(r["stopped"])
            )
            for r in rows
        }

    @classmethod
    def quote(cls, db, stay: Stay) -> tuple[NightPrice, ...]:
        days = cls.days(db, stay.room_type, stay.nights)
        if len(days) != len(stay.nights):
            raise BusinessError("部分住宿日尚未开放预订.")
        return tuple(NightPrice(night, days[night].price) for night in stay.nights)

    @staticmethod
    def allocations(db, booking_id):
        return {
            (row["room_type"], row["night"])
            for row in db.execute(
                "SELECT room_type,night FROM allocations WHERE booking_id=?", (booking_id,)
            )
        }

    @classmethod
    def apply(cls, db, booking_id, stay: Stay | None):
        old = cls.allocations(db, booking_id)
        new = {(stay.room_type, night) for night in stay.nights} if stay else set()
        by_type: dict[int, list[str]] = {}
        for room_type, night in sorted(old | new):
            by_type.setdefault(room_type, []).append(night)

        # Check the entire itinerary before changing any row.
        for room_type, nights in by_type.items():
            inventory = cls.days(db, room_type, nights)
            for night in nights:
                item = (room_type, night)
                delta = int(item in new) - int(item in old)
                stock = inventory.get(night)
                if stock is None:
                    raise BusinessError("部分住宿日尚未开放预订.")
                if item in new or delta:
                    stock.check_sale(delta)

        for room_type, night in sorted(old - new):
            db.execute(
                "UPDATE inventory SET occupied=occupied-1 WHERE room_type=? AND night=?",
                (room_type, night),
            )
            db.execute(
                "DELETE FROM allocations WHERE booking_id=? AND room_type=? AND night=?",
                (booking_id, room_type, night),
            )
        for room_type, night in sorted(new - old):
            db.execute(
                "UPDATE inventory SET occupied=occupied+1 WHERE room_type=? AND night=?",
                (room_type, night),
            )
            db.execute("INSERT INTO allocations VALUES(?,?,?)", (booking_id, room_type, night))


class CatalogQuery:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def room_type(db, room_type):
        return one(
            db,
            """
            SELECT t.*,h.name AS hotel,h.city,h.address,h.photo,h.closed
            FROM room_types t JOIN hotels h ON h.id=t.hotel_id WHERE t.id=?
        """,
            (room_type,),
            "房型不存在.",
        )

    def detail(self, stay: Stay):
        with self.database.read() as db:
            room = self.room_type(db, stay.room_type)
            prices = InventoryService.quote(db, stay)
            return room, prices

    def search(self, start, end, guests=1, city="", room="", minimum=0, maximum=3_000_000_000):
        nights = (end - start).days
        if not 1 <= nights <= 30:
            raise BusinessError("住宿区间须为 1 至 30 晚.", 400)
        if not 1 <= guests <= 12 or not 0 <= minimum <= maximum:
            raise BusinessError("人数或价格区间无效.", 400)
        sql = f"""
            WITH available AS (
                SELECT i.room_type,i.price,i.total-i.occupied-{REPAIR_COUNT} AS remaining
                FROM inventory i JOIN room_types t ON t.id=i.room_type
                JOIN hotels h ON h.id=t.hotel_id
                WHERE i.night>=? AND i.night<? AND i.stopped=0 AND h.closed=0
                  AND t.max_guests>=? AND (?='' OR h.city=?)
                  AND (?='' OR instr(t.name,?)>0)
            )
            SELECT t.id,t.name,t.max_guests,h.id AS hotel_id,h.name AS hotel,h.city,h.address,h.photo,
                   SUM(a.price) AS total, MIN(a.remaining) AS remaining
            FROM available a JOIN room_types t ON t.id=a.room_type
            JOIN hotels h ON h.id=t.hotel_id
            GROUP BY t.id
            HAVING COUNT(*)=? AND MIN(a.remaining)>0 AND SUM(a.price) BETWEEN ? AND ?
            ORDER BY total,t.id LIMIT 300
        """
        with self.database.read() as db:
            return db.execute(
                sql,
                (
                    start.isoformat(),
                    end.isoformat(),
                    guests,
                    city,
                    city,
                    room,
                    room,
                    nights,
                    minimum,
                    maximum,
                ),
            ).fetchall()

    def suggested_dates(self, today):
        with self.database.read() as db:
            first = db.execute(
                "SELECT MIN(night) FROM inventory WHERE night>=?",
                (today.isoformat(),),
            ).fetchone()[0]
        start = today if first is None else max(today, today.fromisoformat(first))
        return start, start + timedelta(days=2)
