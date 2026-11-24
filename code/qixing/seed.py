"""Repeatable fictional data for an offline demonstration."""

from datetime import timedelta

from .access import hash_password
from .domain import Role, local_date

DEMO_PASSWORD = "qixing-demo"
DEMO_USERS = [
    (1, "guest", "林玥", Role.CUSTOMER),
    (2, "guest2", "陈晨", Role.CUSTOMER),
    (3, "staff", "酒店前台", Role.STAFF),
    (4, "admin", "平台管理员", Role.ADMIN),
    (5, "staff2", "酒店运营", Role.STAFF),
]
DEMO_HOTELS = [
    (1, "栖行 · 梧桐里", "南京", "秦淮区 · 老城巷弄", "hotel-1.jpg"),
    (2, "栖行 · 江畔居", "南京", "鼓楼区 · 江畔步道", "hotel-2.jpg"),
    (3, "栖行 · 西湖小筑", "杭州", "西湖区 · 山径一侧", "hotel-3.jpg"),
]
DEMO_TYPES = [
    (1, 1, "庭院大床房", 2),
    (2, 1, "临窗双床房", 2),
    (3, 2, "江景大床房", 2),
    (4, 2, "家庭套房", 4),
    (5, 3, "山景大床房", 2),
    (6, 3, "庭园双床房", 2),
]


def seed(database, today=None):
    today = today or local_date()
    with database.read() as db:
        if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return False
    users = [
        (ident, username, name, hash_password(DEMO_PASSWORD), role)
        for ident, username, name, role in DEMO_USERS
    ]
    with database.transaction() as db:
        if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return False
        db.executemany("INSERT INTO users VALUES(?,?,?,?,?)", users)
        db.executemany(
            "INSERT INTO hotels(id,name,city,address,photo) VALUES(?,?,?,?,?)", DEMO_HOTELS
        )
        db.executemany("INSERT INTO grants VALUES(?,?)", [(3, 1), (5, 2), (5, 3)])
        db.executemany("INSERT INTO room_types VALUES(?,?,?,?)", DEMO_TYPES)
        for room_type, hotel, _, _ in DEMO_TYPES:
            db.executemany(
                "INSERT INTO rooms(hotel_id,room_type,number) VALUES(?,?,?)",
                [(hotel, room_type, f"{room_type}0{i}") for i in range(1, 4)],
            )
            db.executemany(
                "INSERT INTO inventory(room_type,night,total,price) VALUES(?,?,?,?)",
                [
                    (
                        room_type,
                        (today + timedelta(days=i)).isoformat(),
                        3,
                        29_000
                        + room_type * 4_500
                        + (5_000 if (today.weekday() + i) % 7 >= 5 else 0),
                    )
                    for i in range(90)
                ],
            )
    return True
