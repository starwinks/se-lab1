"""Lab 1 REQ-031. Time 100 correct HTTP searches with 20 concurrent clients."""

import json
import platform
import sqlite3
import statistics
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from threading import Barrier, Thread
from time import perf_counter
from urllib.parse import urlencode
from urllib.request import urlopen

from qixing.domain import day_bounds
from qixing.storage import Database
from qixing.web import Application, Server

START = date(2026, 11, 24)


def generate(database):
    created, _ = day_bounds(str(START), str(START))
    end = START + timedelta(days=2)
    with database.transaction() as db:
        db.execute("INSERT INTO users VALUES(1,'benchmark','基准顾客','unused','customer')")
        db.executemany(
            "INSERT INTO hotels(id,name,city,address) VALUES(?,?,?,?)",
            [
                (i, f"样例酒店{i:03}", "南京" if i % 2 else "杭州", "演示地址")
                for i in range(1, 101)
            ],
        )
        db.executemany(
            "INSERT INTO room_types VALUES(?,?,?,?)",
            [(i, (i - 1) // 5 + 1, f"房型{(i - 1) % 5 + 1}", 2) for i in range(1, 501)],
        )
        db.executemany(
            "INSERT INTO inventory(room_type,night,total,price) VALUES(?,?,?,?)",
            [
                (i, str(START + timedelta(days=offset)), 5, 25_000 + i * 20)
                for i in range(1, 501)
                for offset in range(30)
            ],
        )
        db.executemany(
            """INSERT INTO bookings(customer,hotel_id,room_type,arrival,departure,guest,
               guests,total,state,version,created,waiting_since) VALUES(1,?,?,?,?,?,?,?,?,1,?,?)""",
            [
                (
                    (i - 1) // 5 + 1,
                    i,
                    str(START),
                    str(end),
                    "基准顾客",
                    1,
                    (25_000 + i * 20) * 2,
                    "已取消",
                    created,
                    created,
                )
                for i in range(1, 501)
                for _ in range(20)
            ],
        )
    with database.read() as db:
        return [
            db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("hotels", "room_types", "bookings", "inventory")
        ]


@contextmanager
def serving(database):
    with Server(("127.0.0.1", 0), Application(database)) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


def run(url):
    gates = Barrier(20)
    criteria = urlencode(
        {
            "city": "南京",
            "room": "房型1",
            "start": START,
            "end": START + timedelta(days=2),
            "guests": 2,
            "min": 500,
            "max": 700,
        }
    )
    expected_ids = [((n - 1) * 5 + 1) for n in range(1, 101, 2)]

    def client(_):
        gates.wait(timeout=10)
        samples = []
        for _ in range(5):
            started = perf_counter()
            correct = False
            try:
                with urlopen(f"{url}/api/search?{criteria}", timeout=5) as response:
                    data = json.load(response)
                correct = [row["id"] for row in data] == expected_ids and all(
                    row["total"] == (25_000 + row["id"] * 20) * 2 and row["remaining"] == 5
                    for row in data
                )
            except (OSError, ValueError, KeyError, TypeError):
                pass
            samples.append((perf_counter() - started, correct))
        return samples

    with ThreadPoolExecutor(max_workers=20) as pool:
        return [item for group in pool.map(client, range(20)) for item in group]


def main():
    with tempfile.TemporaryDirectory() as temporary:
        database = Database(Path(temporary) / "benchmark.sqlite")
        database.initialize()
        counts = generate(database)
        with serving(database) as url:
            samples = run(url)
    latencies = sorted(duration * 1000 for duration, _ in samples)
    result = {
        "system": platform.platform(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "hotels_room_types_orders_inventory": counts,
        "requests": len(samples),
        "correct": sum(correct for _, correct in samples),
        "correct_within_2s": sum(correct and elapsed <= 2 for elapsed, correct in samples),
        "median_ms": round(statistics.median(latencies), 2),
        "p95_ms": round(latencies[94], 2),
        "max_ms": round(latencies[-1], 2),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["correct_within_2s"] < 95:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
