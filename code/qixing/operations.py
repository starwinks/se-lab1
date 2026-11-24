"""Front desk, hotel maintenance and grant administration."""

from .access import Authorizer
from .booking import booking_row, model
from .catalog import CatalogQuery
from .domain import BookingState, BusinessError, Role, RoomState
from .storage import one, snapshot


def room_row(db, room_id):
    return one(db, "SELECT * FROM rooms WHERE id=?", (room_id,), "客房不存在.")


class FrontDeskService:
    def __init__(self, database):
        self.database = database

    def candidates(self, actor, booking_id):
        with self.database.read() as db:
            row = booking_row(db, booking_id)
            Authorizer.hotel(db, actor, row["hotel_id"])
            model(db, row).require(BookingState.CONFIRMED)
            return db.execute(
                """SELECT r.* FROM rooms r WHERE hotel_id=? AND room_type=? AND state='空闲'
                   AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.room_id=r.id AND ended IS NULL)
                   ORDER BY number""",
                (row["hotel_id"], row["room_type"]),
            ).fetchall()

    def check_in(self, actor, booking_id, room_id):
        with self.database.write(actor, "办理入住", f"订单 {booking_id}") as (db, change):
            row = booking_row(db, booking_id)
            Authorizer.hotel(db, actor, row["hotel_id"])
            order = model(db, row)
            order.require(BookingState.CONFIRMED)
            room = room_row(db, room_id)
            if (room["hotel_id"], room["room_type"]) != (
                order.hotel,
                order.revision.stay.room_type,
            ):
                raise BusinessError("请选择同一家酒店的同房型客房.")
            change.before = {"order": snapshot(row), "room": snapshot(room)}
            if (
                room["state"] != RoomState.FREE
                or db.execute(
                    "SELECT 1 FROM assignments WHERE ended IS NULL AND (room_id=? OR booking_id=?)",
                    (room_id, booking_id),
                ).fetchone()
            ):
                raise BusinessError("房间已不可分配, 请刷新可选房间.")
            db.execute(
                "INSERT INTO assignments(booking_id,room_id,started) VALUES(?,?,?)",
                (booking_id, room_id, self.database.clock()),
            )
            db.execute("UPDATE rooms SET state=? WHERE id=?", (RoomState.OCCUPIED, room_id))
            db.execute(
                "UPDATE bookings SET state=? WHERE id=?", (BookingState.IN_HOUSE, booking_id)
            )
            change.after = {
                "order": snapshot(booking_row(db, booking_id)),
                "room": snapshot(room_row(db, room_id)),
            }

    def check_out(self, actor, booking_id):
        with self.database.write(actor, "办理退房", f"订单 {booking_id}") as (db, change):
            row = booking_row(db, booking_id)
            Authorizer.hotel(db, actor, row["hotel_id"])
            model(db, row).require(BookingState.IN_HOUSE)
            assignment = one(
                db,
                "SELECT * FROM assignments WHERE booking_id=? AND ended IS NULL",
                (booking_id,),
                "入住分配不存在.",
            )
            room_id = assignment["room_id"]
            change.before = {"order": snapshot(row), "room": snapshot(room_row(db, room_id))}
            db.execute(
                "UPDATE assignments SET ended=? WHERE id=?",
                (self.database.clock(), assignment["id"]),
            )
            db.execute(
                "UPDATE bookings SET state=? WHERE id=?", (BookingState.COMPLETED, booking_id)
            )
            db.execute("UPDATE rooms SET state=? WHERE id=?", (RoomState.DIRTY, room_id))
            change.after = {
                "order": snapshot(booking_row(db, booking_id)),
                "room": snapshot(room_row(db, room_id)),
            }

    def clean(self, actor, room_id):
        with self.database.write(actor, "完成清洁", f"客房 {room_id}") as (db, change):
            row = room_row(db, room_id)
            Authorizer.hotel(db, actor, row["hotel_id"])
            change.before = snapshot(row)
            if row["state"] != RoomState.DIRTY:
                raise BusinessError("仅待清洁房间可执行此操作.")
            db.execute("UPDATE rooms SET state=? WHERE id=?", (RoomState.FREE, room_id))
            change.after = snapshot(room_row(db, room_id))
            return row["hotel_id"]

    def repair(self, actor, room_id, first, last, reason):
        if last < first or (last - first).days > 366:
            raise BusinessError("维修日期范围无效.", 400)
        with self.database.write(actor, "开始维修", f"客房 {room_id}") as (db, change):
            row = room_row(db, room_id)
            Authorizer.hotel(db, actor, row["hotel_id"])
            change.before = snapshot(row)
            if row["state"] != RoomState.FREE:
                raise BusinessError("只有空闲客房可开始维修.")
            cur = db.execute(
                """INSERT INTO repairs(room_id,room_type,first_night,last_night,reason)
                   VALUES(?,?,?,?,?)""",
                (room_id, row["room_type"], first.isoformat(), last.isoformat(), reason),
            )
            db.execute("UPDATE rooms SET state=? WHERE id=?", (RoomState.REPAIR, room_id))
            conflicts = db.execute(
                """SELECT DISTINCT a.booking_id FROM allocations a
                   JOIN inventory i USING(room_type,night) JOIN bookings b ON b.id=a.booking_id
                   WHERE i.room_type=? AND i.night BETWEEN ? AND ?
                     AND b.state IN ('待确认','已确认','已入住')
                     AND i.occupied > i.total - (
                       SELECT COUNT(*) FROM repairs r WHERE r.room_type=i.room_type
                       AND r.ended IS NULL AND i.night BETWEEN r.first_night AND r.last_night
                     )""",
                (row["room_type"], first.isoformat(), last.isoformat()),
            ).fetchall()
            db.executemany(
                "INSERT OR IGNORE INTO coordination(booking_id,reason,opened) VALUES(?,?,?)",
                [
                    (b["booking_id"], f"维修 #{cur.lastrowid}: {reason}", self.database.clock())
                    for b in conflicts
                ],
            )
            change.after = {"room": snapshot(room_row(db, room_id)), "conflicts": len(conflicts)}
            return row["hotel_id"]

    def restore(self, actor, room_id):
        with self.database.write(actor, "确认维修完成", f"客房 {room_id}") as (db, change):
            row = room_row(db, room_id)
            Authorizer.hotel(db, actor, row["hotel_id"])
            change.before = snapshot(row)
            if row["state"] != RoomState.REPAIR:
                raise BusinessError("所选客房不在维修中.")
            active = db.execute(
                "UPDATE repairs SET ended=? WHERE room_id=? AND ended IS NULL",
                (self.database.clock(), room_id),
            )
            if active.rowcount != 1:
                raise BusinessError("维修记录不完整.")
            db.execute("UPDATE rooms SET state=? WHERE id=?", (RoomState.FREE, room_id))
            change.after = snapshot(room_row(db, room_id))
            return row["hotel_id"]


class HotelService:
    def __init__(self, database):
        self.database = database

    def save_hotel(self, actor, name, city, address, hotel_id=None):
        with self.database.write(actor, "维护酒店", f"酒店 {hotel_id or '新增'}") as (db, change):
            Authorizer.role(actor, Role.ADMIN)
            if hotel_id:
                change.before = snapshot(one(db, "SELECT * FROM hotels WHERE id=?", (hotel_id,)))
                db.execute(
                    "UPDATE hotels SET name=?,city=?,address=? WHERE id=?",
                    (name, city, address, hotel_id),
                )
            else:
                cur = db.execute(
                    "INSERT INTO hotels(name,city,address) VALUES(?,?,?)", (name, city, address)
                )
                hotel_id = cur.lastrowid
            change.target = f"酒店 {hotel_id}"
            change.after = snapshot(one(db, "SELECT * FROM hotels WHERE id=?", (hotel_id,)))
            return hotel_id

    def close(self, actor, hotel_id, closed, reason):
        with self.database.write(actor, "停业" if closed else "恢复营业", f"酒店 {hotel_id}") as (
            db,
            change,
        ):
            Authorizer.role(actor, Role.ADMIN)
            row = one(db, "SELECT * FROM hotels WHERE id=?", (hotel_id,))
            change.before = snapshot(row)
            if bool(row["closed"]) == closed:
                raise BusinessError("酒店已处于所选状态.")
            db.execute("UPDATE hotels SET closed=? WHERE id=?", (int(closed), hotel_id))
            if closed:
                db.execute(
                    """INSERT OR IGNORE INTO coordination(booking_id,reason,opened)
                       SELECT id,?,? FROM bookings WHERE hotel_id=?
                       AND state IN ('待确认','已确认','已入住')""",
                    (f"酒店停业: {reason}", self.database.clock(), hotel_id),
                )
            change.detail = reason
            change.after = snapshot(one(db, "SELECT * FROM hotels WHERE id=?", (hotel_id,)))

    def resolve(self, actor, case_id, resolution):
        with self.database.write(actor, "记录协调结果", f"协调 {case_id}") as (db, change):
            row = one(
                db,
                """SELECT c.*,b.hotel_id FROM coordination c JOIN bookings b
                             ON b.id=c.booking_id WHERE c.id=?""",
                (case_id,),
            )
            Authorizer.hotel(db, actor, row["hotel_id"], admin=True)
            change.before = snapshot(row)
            if row["resolution"] is not None:
                raise BusinessError("该协调记录已处理.")
            db.execute("UPDATE coordination SET resolution=? WHERE id=?", (resolution, case_id))
            change.after = {**dict(row), "resolution": resolution}

    def save_room_type(self, actor, hotel_id, name, max_guests, room_type=None):
        with self.database.write(actor, "维护房型", f"酒店 {hotel_id}") as (db, change):
            Authorizer.hotel(db, actor, hotel_id)
            one(db, "SELECT id FROM hotels WHERE id=?", (hotel_id,))
            if room_type:
                row = CatalogQuery.room_type(db, room_type)
                if row["hotel_id"] != hotel_id:
                    raise BusinessError("房型不属于该酒店.", 403)
                change.before = snapshot(row)
                db.execute(
                    "UPDATE room_types SET name=?,max_guests=? WHERE id=?",
                    (name, max_guests, room_type),
                )
            else:
                cur = db.execute(
                    "INSERT INTO room_types(hotel_id,name,max_guests) VALUES(?,?,?)",
                    (hotel_id, name, max_guests),
                )
                room_type = cur.lastrowid
            change.target = f"房型 {room_type}"
            change.after = snapshot(CatalogQuery.room_type(db, room_type))

    def add_room(self, actor, room_type, number):
        with self.database.write(actor, "登记客房", f"房型 {room_type}") as (db, change):
            item = CatalogQuery.room_type(db, room_type)
            Authorizer.hotel(db, actor, item["hotel_id"])
            cur = db.execute(
                "INSERT INTO rooms(hotel_id,room_type,number) VALUES(?,?,?)",
                (item["hotel_id"], room_type, number),
            )
            change.target = f"客房 {cur.lastrowid}"
            change.after = snapshot(room_row(db, cur.lastrowid))
            return item["hotel_id"]

    def save_inventory(self, actor, room_type, night, total, price, stopped):
        with self.database.write(actor, "维护每日售卖", f"房型 {room_type} / {night}") as (
            db,
            change,
        ):
            item = CatalogQuery.room_type(db, room_type)
            Authorizer.hotel(db, actor, item["hotel_id"])
            parameters = (room_type, night)
            sql = "SELECT * FROM inventory WHERE room_type=? AND night=?"
            row = db.execute(sql, parameters).fetchone()
            change.before = snapshot(row)
            if row is not None and total < row["occupied"]:
                raise BusinessError("库存总量不能低于已占用量.")
            db.execute(
                """INSERT INTO inventory(room_type,night,total,price,stopped) VALUES(?,?,?,?,?)
                   ON CONFLICT(room_type,night) DO UPDATE
                   SET total=excluded.total,price=excluded.price,stopped=excluded.stopped""",
                (*parameters, total, price, int(stopped)),
            )
            change.after = snapshot(one(db, sql, parameters))
            return item["hotel_id"]


class AuthorizationService:
    def __init__(self, database):
        self.database = database

    def set_grant(self, actor, user, hotel, allowed):
        with self.database.write(actor, "调整酒店授权", f"人员 {user} / 酒店 {hotel}") as (
            db,
            change,
        ):
            Authorizer.role(actor, Role.ADMIN)
            staff = one(db, "SELECT role FROM users WHERE id=?", (user,))
            if staff["role"] != Role.STAFF:
                raise BusinessError("只能为酒店员工维护酒店授权.")
            one(db, "SELECT id FROM hotels WHERE id=?", (hotel,))
            parameters = (user, hotel)
            prior = db.execute("SELECT 1 FROM grants WHERE user_id=? AND hotel_id=?", parameters)
            change.before = {"allowed": prior.fetchone() is not None}
            if allowed:
                db.execute("INSERT OR IGNORE INTO grants VALUES(?,?)", parameters)
            else:
                db.execute("DELETE FROM grants WHERE user_id=? AND hotel_id=?", parameters)
            change.after = {"allowed": allowed}
