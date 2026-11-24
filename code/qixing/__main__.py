"""Command-line entry point for the Qixing prototype."""

import argparse
import logging
import sqlite3
from pathlib import Path
from threading import Event, Thread

from .booking import BookingService
from .seed import seed
from .storage import Database
from .web import Application, Server

LOG = logging.getLogger(__name__)


def maintenance(database: Database, stopped: Event):
    booking = BookingService(database)
    while not stopped.is_set():
        try:
            booking.run_expiry()
            database.replay_failures()
        except sqlite3.Error:
            LOG.exception("后台维护暂时失败")
        stopped.wait(2)


def main():
    parser = argparse.ArgumentParser(description="栖行酒店预订系统")
    parser.add_argument("--db", type=Path, default=Path("data/qixing.sqlite3"), help="数据库位置")
    parser.add_argument("--port", type=int, default=8767, help="本地浏览器端口")
    parser.add_argument("--demo", action="store_true", help="在空数据库中初始化演示资料")
    parser.add_argument("--init-only", action="store_true", help="初始化数据库后退出")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("端口必须在 0 至 65535 之间.")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    database = Database(args.db)
    database.initialize()
    if args.demo:
        seed(database)
    if args.init_only:
        return
    stopped = Event()
    try:
        server = Server(("127.0.0.1", args.port), Application(database))
    except OSError as error:
        parser.exit(1, f"无法绑定本地端口: {error}\n")
    with server:
        worker = Thread(target=maintenance, args=(database, stopped), daemon=True)
        worker.start()
        print(f"栖行正在运行: http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stopped.set()
            worker.join(timeout=15)


if __name__ == "__main__":
    main()
