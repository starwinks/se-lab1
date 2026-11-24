"""Local HTTP adapter. Domain commands remain usable independently of the browser."""

import hmac
import json
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from threading import Lock
from urllib.parse import parse_qs, urlsplit

from . import views
from .access import Authorizer
from .booking import BookingService
from .catalog import CatalogQuery
from .domain import (
    BookingState,
    BusinessError,
    RequestState,
    Role,
    Stay,
    cents,
    date_key,
    integer,
    local_date,
    required,
)
from .operations import AuthorizationService, FrontDeskService, HotelService
from .reporting import ReportingService

LOG = logging.getLogger(__name__)
SESSION_SECONDS = 8 * 3600


@dataclass(frozen=True)
class Session:
    user: int | None
    csrf: str
    expires: float


class Sessions:
    def __init__(self):
        self.data = {}
        self.lock = Lock()

    def find(self, token):
        with self.lock:
            record = self.data.get(token)
            return record if record and record.expires > time.monotonic() else None

    def replace(self, old="", user=None):
        with self.lock:
            now = time.monotonic()
            self.data.pop(old, None)
            if len(self.data) >= 2048:
                self.data = {k: v for k, v in self.data.items() if v.expires > now}
                if len(self.data) >= 2048:
                    self.data.pop(next(iter(self.data)))
            token = secrets.token_urlsafe(32)
            session = Session(user, secrets.token_urlsafe(24), now + SESSION_SECONDS)
            self.data[token] = session
            return token, session


class Fields(dict):
    def id(self, key="id", *, optional=False):
        value = self.get(key, "")
        if optional and not value:
            return None
        return integer(value, key, maximum=2**63 - 1)

    def text(self, key, label, limit=100):
        return required(self.get(key, ""), label, limit)

    def date(self, key):
        return date_key(self.get(key, ""))

    def toggle(self, key):
        return bool(integer(self.get(key, ""), key, 0, 1))

    def stay(self, fallback=""):
        return Stay(
            self.id("room_type"),
            self.date("start"),
            self.date("end"),
            integer(self.get("guests", "1"), "入住人数", 1, 12),
            required(self.get("guest", fallback), "入住人", 80),
        )


class Application:
    def __init__(self, database):
        self.database = database
        self.sessions = Sessions()
        self.auth = Authorizer(database)
        self.catalog = CatalogQuery(database)
        self.booking = BookingService(database)
        self.desk = FrontDeskService(database)
        self.hotels = HotelService(database)
        self.grants = AuthorizationService(database)
        self.report = ReportingService(database)
        self.pages = {
            "/": self.home,
            "/offer": self.offer,
            "/orders": self.orders,
            "/order": self.order,
            "/inventory": self.inventory,
            "/rooms": self.rooms,
            "/types": self.types,
            "/hotels": self.hotel_page,
            "/grants": self.grant_page,
            "/stats": self.stats,
            "/audit": self.audit,
            "/coordination": self.coordination,
        }

    def home(self, q, actor, csrf):
        if actor and actor.role != Role.CUSTOMER:
            return "订单工作台", self.orders(q, actor, csrf)[1]
        criteria, rows = self.search(q)
        return "查找酒店", views.search(criteria, rows, filtered=bool(q))

    def search(self, q):
        criteria = {name: q.get(name, "") for name in ("city", "room", "min", "max")}
        if "start" in q or "end" in q:
            start, end = q.date("start"), q.date("end")
        else:
            start, end = self.catalog.suggested_dates(local_date())
        guests = integer(q.get("guests", "1"), "入住人数", 1, 12)
        rows = self.catalog.search(
            start,
            end,
            guests,
            criteria["city"].strip(),
            criteria["room"].strip(),
            cents(criteria["min"]) if criteria["min"] else 0,
            cents(criteria["max"]) if criteria["max"] else 3_000_000_000,
        )
        criteria.update(start=start.isoformat(), end=end.isoformat(), guests=guests)
        return criteria, rows

    def offer(self, q, actor, csrf):
        stay = q.stay(actor.name if actor else "顾客")
        item, prices = self.catalog.detail(stay)
        return item["hotel"], views.offer(
            item, stay, prices, actor, csrf, secrets.token_urlsafe(16)
        )

    @staticmethod
    def filter_args(q):
        return q.id("hotel", optional=True), q.get("from", ""), q.get("to", "")

    def orders(self, q, actor, csrf):
        hotels = self.report.hotels(actor) if actor.role != Role.CUSTOMER else []
        rows = self.report.orders(actor, *self.filter_args(q), state=q.get("state", ""))
        title = "我的订单" if actor.role == Role.CUSTOMER else "订单工作台"
        return title, views.orders(rows, views.order_filters(hotels, q), actor, q)

    def order(self, q, actor, csrf):
        booking_id = q.id()
        data = self.report.detail(actor, booking_id)
        row = data[0]
        candidates, types = (), ()
        if actor.role == Role.STAFF and row["state"] == BookingState.CONFIRMED:
            candidates = self.desk.candidates(actor, booking_id)
        if actor.role == Role.CUSTOMER:
            # Viewing one's own booking allows only this hotel's room-type options.
            with self.database.read() as db:
                types = db.execute(
                    "SELECT id,name FROM room_types WHERE hotel_id=? ORDER BY name",
                    (row["hotel_id"],),
                ).fetchall()
        title = f"订单 #{booking_id}"
        return title, views.detail(
            data, actor, csrf, candidates, [(t["id"], t["name"]) for t in types]
        )

    def selected_hotel(self, q, actor):
        Authorizer.role(actor, Role.STAFF)
        hotels = self.report.hotels(actor)
        selected = q.id("hotel", optional=True)
        if selected is None and hotels:
            selected = hotels[0]["id"]
        if selected is None:
            raise BusinessError("当前账号尚未获得酒店授权.", 403)
        return hotels, selected

    def inventory(self, q, actor, csrf):
        hotels, selected = self.selected_hotel(q, actor)
        first = q.date("first") if q.get("first") else local_date()
        return "每日库存与房价", views.inventory_page(
            hotels,
            selected,
            self.report.room_types(actor, selected),
            self.report.stock(actor, selected, first.isoformat()),
            first,
            csrf,
            q.id("edit", optional=True),
            q.get("night", ""),
        )

    def rooms(self, q, actor, csrf):
        hotels, selected = self.selected_hotel(q, actor)
        return "实体客房", views.rooms_page(
            hotels,
            selected,
            self.report.rooms(actor, selected),
            self.report.room_types(actor, selected),
            local_date(),
            csrf,
        )

    def types(self, q, actor, csrf):
        hotels, selected = self.selected_hotel(q, actor)
        return "房型资料", views.types_page(
            hotels, selected, self.report.room_types(actor, selected), csrf
        )

    def hotel_page(self, q, actor, csrf):
        Authorizer.role(actor, Role.ADMIN)
        return "平台酒店资料", views.hotels_page(self.report.hotels(actor), csrf)

    def grant_page(self, q, actor, csrf):
        rows = self.report.staff_grants(actor)
        return "人员与酒店授权", views.grants_page(rows, self.report.hotels(actor), csrf)

    def stats(self, q, actor, csrf):
        totals, breakdown = self.report.summary(actor, *self.filter_args(q))
        filters = views.order_filters(self.report.hotels(actor), q, summary=True)
        return "订单业务统计", views.stats(totals, breakdown, filters)

    def audit(self, q, actor, csrf):
        rows = self.report.audit(
            actor,
            q.get("target", ""),
            q.get("outcome", ""),
            q.get("from", ""),
            q.get("to", ""),
            q.get("action", ""),
        )
        return "关键操作日志", views.audit(rows, q)

    def coordination(self, q, actor, csrf):
        return "待协调订单", views.coordination_page(
            self.report.coordination(actor, q.id("hotel", optional=True)),
            csrf,
        )

    def submit(self, actor, data, csrf):
        key = data.text("key", "请求编号", 80)
        stay = data.stay()
        result = self.booking.submit(
            actor, key, stay, data.get("decision", ""), data.get("challenge", "")
        )
        if result.state == RequestState.WAITING:
            return "确认相同行程", views.duplicate(stay, result, csrf, key)
        if result.state == RequestState.DECLINED:
            return views.href("/orders", notice="已放弃本次请求.")
        return views.href("/order", id=result.booking_id, notice="预订已提交, 等待酒店确认.")

    def order_command(self, path, actor, data):
        ident = data.id()
        if path == "/modify":
            self.booking.modify(actor, ident, data.stay())
        elif path == "/cancel":
            self.booking.cancel(actor, ident)
        elif path == "/confirm":
            self.booking.confirm(actor, ident)
        elif path == "/check-in":
            self.desk.check_in(actor, ident, data.id("room"))
        else:
            self.desk.check_out(actor, ident)
        return views.href("/order", id=ident, notice="操作已保存.")

    def room_command(self, path, actor, data):
        ident = data.id()
        if path == "/clean":
            hotel = self.desk.clean(actor, ident)
        elif path == "/restore":
            hotel = self.desk.restore(actor, ident)
        else:
            hotel = self.desk.repair(
                actor, ident, data.date("first"), data.date("last"), data.text("reason", "维修原因")
            )
        return views.href("/rooms", hotel=hotel, notice="客房状态已更新.")

    def command(self, path, actor, data, csrf):
        if path == "/book":
            return self.submit(actor, data, csrf)
        if path in {"/modify", "/cancel", "/confirm", "/check-in", "/check-out"}:
            return self.order_command(path, actor, data)
        if path in {"/clean", "/restore", "/repair"}:
            return self.room_command(path, actor, data)
        if path == "/stock":
            room_type = data.id("room_type")
            hotel = self.hotels.save_inventory(
                actor,
                room_type,
                data.date("night").isoformat(),
                integer(data.get("total", ""), "库存总量", 0, 10_000),
                cents(data.get("price", "")),
                data.toggle("stopped"),
            )
            return views.href(
                "/inventory", hotel=hotel, first=data["night"], notice="售卖信息已保存."
            )
        if path == "/type-save":
            hotel = data.id("hotel")
            self.hotels.save_room_type(
                actor,
                hotel,
                data.text("name", "房型名称", 80),
                integer(data.get("max_guests", ""), "最多人数", 1, 12),
                data.id(optional=True),
            )
            return views.href("/types", hotel=hotel, notice="房型已保存.")
        if path == "/room-save":
            hotel = self.hotels.add_room(
                actor, data.id("room_type"), data.text("number", "房号", 20)
            )
            return views.href("/rooms", hotel=hotel, notice="客房已登记.")
        if path == "/hotel-save":
            self.hotels.save_hotel(
                actor,
                data.text("name", "酒店名称"),
                data.text("city", "城市"),
                data.text("address", "地址"),
                data.id(optional=True),
            )
            return views.href("/hotels", notice="酒店资料已保存.")
        if path == "/hotel-status":
            self.hotels.close(actor, data.id(), data.toggle("closed"), data.text("reason", "原因"))
            return views.href("/hotels", notice="经营状态已保存.")
        if path == "/grant":
            self.grants.set_grant(actor, data.id("user"), data.id("hotel"), data.toggle("allowed"))
            return views.href("/grants", notice="授权已更新.")
        if path == "/resolve":
            self.hotels.resolve(actor, data.id(), data.text("resolution", "处理说明"))
            return views.href("/coordination", notice="协调结果已保存.")
        raise BusinessError("功能入口不存在.", 404)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "Qixing"

    def setup(self):
        super().setup()
        self.connection.settimeout(12)

    def log_message(self, fmt, *args):
        LOG.debug("%s %s", self.address_string(), fmt % args)

    @property
    def app(self):
        return self.server.app

    def respond(self, status, body=b"", content_type="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
        )
        if extra:
            for name, value in extra.items():
                self.send_header(name, value)
        if self.pending_cookie:
            self.send_header(
                "Set-Cookie", f"qixing={self.pending_cookie}; Path=/; HttpOnly; SameSite=Strict"
            )
        self.end_headers()
        self.wfile.write(body)

    def html(self, title, body, status=200, message="", landing=False):
        self.respond(
            status,
            views.page(
                title,
                body,
                self.actor,
                self.session.csrf if self.session else "",
                message=message,
                landing=landing,
                path=getattr(self, "current_path", ""),
            ),
        )

    def redirect(self, target):
        self.respond(303, extra={"Location": target})

    def parse(self, query):
        try:
            values = parse_qs(query, keep_blank_values=True, max_num_fields=40, errors="strict")
        except (ValueError, UnicodeError) as error:
            raise BusinessError("表单格式无效.", 400) from error
        if any(len(items) != 1 for items in values.values()):
            raise BusinessError("表单字段不能重复.", 400)
        return Fields({key: items[0] for key, items in values.items()})

    def session_for_request(self):
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except CookieError:
            return "", None
        token = jar["qixing"].value if "qixing" in jar else ""
        return token, self.app.sessions.find(token)

    def dispatch(self, method):
        self.pending_cookie = ""
        self.actor = None
        self.session = None
        path = ""
        try:
            url = urlsplit(self.path)
            path = url.path
            self.current_path = path
            if method == "GET" and path.startswith("/static/"):
                return self.static(path.removeprefix("/static/"))
            token, self.session = self.session_for_request()
            if self.session and self.session.user:
                self.actor = self.app.auth.actor(self.session.user)
            if method == "GET":
                return self.get(path, self.parse(url.query), token)
            self.post(path, token)
        except BusinessError as error:
            self.error(path, error.status, str(error))
        except sqlite3.IntegrityError:
            self.error(path, 409, "数据与现有记录冲突, 本次操作已撤销.")
        except sqlite3.Error:
            LOG.exception("数据库访问失败")
            self.error(path, 503, "暂时无法完成操作, 请稍后重试.")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            LOG.debug("客户端已断开")
        except Exception:
            LOG.exception("处理请求失败")
            self.error(path, 500, "发生内部错误.")

    def error(self, path, status, message):
        if path.startswith("/api/"):
            self.respond(
                status,
                json.dumps({"error": message}, ensure_ascii=False),
                "application/json; charset=utf-8",
            )
            return
        target = "/orders" if self.actor else "/login"
        self.html(
            HTTPStatus(status).phrase if status >= 500 else "操作提示",
            '<div class="actions">'
            + views.link(target, "返回", "button")
            + views.link("/", "首页")
            + "</div>",
            status,
            message=message,
        )

    def static(self, file):
        kind = "image/jpeg" if file in views.PHOTOS else None
        if file == "style.css":
            kind = "text/css; charset=utf-8"
        if kind is None:
            raise BusinessError("资源不存在.", 404)
        contents = files("qixing").joinpath("static", file).read_bytes()
        self.respond(200, contents, kind)

    def get(self, path, query, token):
        if path == "/login":
            if self.actor:
                return self.redirect("/orders" if self.actor.role != Role.CUSTOMER else "/")
            if not self.session:
                self.pending_cookie, self.session = self.app.sessions.replace(token)
            target = self.offer_return(query.get("next", ""))
            return self.html("登录", views.login(self.session.csrf, target), landing=True)
        if path == "/api/search":
            _, rows = self.app.search(query)
            return self.respond(
                200,
                json.dumps([dict(row) for row in rows], ensure_ascii=False),
                "application/json; charset=utf-8",
            )
        page = self.app.pages.get(path)
        if page is None:
            raise BusinessError("页面不存在.", 404)
        if path not in {"/", "/offer"} and not self.actor:
            return self.redirect("/login")
        title, body = page(query, self.actor, self.session.csrf if self.session else "")
        landing = path == "/offer" or (
            path == "/" and (not self.actor or self.actor.role == Role.CUSTOMER)
        )
        return self.html(title, body, message=query.get("notice", "")[:160], landing=landing)

    def offer_return(self, value):
        try:
            target = urlsplit(value)
            if target.scheme or target.netloc or target.path != "/offer":
                return ""
            fields = self.parse(target.query)
            stay = fields.stay("顾客")
        except (ValueError, BusinessError):
            return ""
        return views.href(
            "/offer",
            room_type=stay.room_type,
            start=stay.arrival,
            end=stay.departure,
            guests=stay.guests,
        )

    def post(self, path, token):
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            raise BusinessError("不支持此表单格式.", 415)
        size = integer(self.headers.get("Content-Length", ""), "表单大小", 1, 12_288)
        data = self.parse(self.rfile.read(size).decode("ascii", errors="replace"))
        if not self.session or not hmac.compare_digest(
            data.get("csrf", "").encode(), self.session.csrf.encode()
        ):
            raise BusinessError("表单会话已失效, 请刷新后重新提交.", 403)
        if path == "/login":
            actor = self.app.auth.login(
                data.text("username", "账号", 60), data.text("password", "密码", 128)
            )
            self.pending_cookie, _ = self.app.sessions.replace(token, actor.id)
            destination = "/orders"
            if actor.role == Role.CUSTOMER:
                destination = self.offer_return(data.get("next", "")) or "/"
            return self.redirect(destination)
        if not self.actor:
            raise BusinessError("请先登录.", 401)
        if path == "/logout":
            self.pending_cookie, _ = self.app.sessions.replace(token)
            return self.redirect("/login")
        result = self.app.command(path, self.actor, data, self.session.csrf)
        if isinstance(result, tuple):
            return self.html(*result)
        return self.redirect(result)

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")
