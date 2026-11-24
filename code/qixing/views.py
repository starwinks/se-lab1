"""Server-rendered pages for the desktop browser interface."""

from functools import lru_cache
from html import escape
from importlib.resources import files
from string import Template
from urllib.parse import urlencode

from .domain import (
    CONFIRM_WINDOW,
    BookingState,
    Role,
    RoomState,
    Stay,
    date_key,
    display_time,
    money,
)

PHOTOS = {f"hotel-{i}.jpg" for i in range(1, 4)} | {"login-room.jpg", "hero-room.jpg"}
ROLE_NAMES = {Role.CUSTOMER: "顾客", Role.STAFF: "酒店工作人员", Role.ADMIN: "平台管理员"}


def e(value):
    return escape(str(value), quote=True)


def href(path, **values):
    return path + ("?" + urlencode(values) if values else "")


@lru_cache
def template(name):
    source = files("qixing").joinpath("templates", name + ".html").read_text(encoding="utf-8")
    return Template(source)


def link(url, title, cls=""):
    style = {
        "button": "btn btn-primary",
        "button secondary": "btn btn-outline-secondary line",
        "back-link": "back-link d-inline-block mb-3",
    }.get(cls, cls)
    return f'<a href="{e(url)}" class="{e(style)}">{e(title)}</a>'


def photo(name, alt, *, lazy=False):
    file = name if name in PHOTOS else "hotel-1.jpg"
    loading = ' loading="lazy"' if lazy else ""
    return f'<img src="/static/{file}" alt="{e(alt)}"{loading}>'


def field(label, name, value="", kind="text", *, required=True, **attributes):
    options = " ".join(f'{e(k.replace("_", "-"))}="{e(v)}"' for k, v in attributes.items())
    required_attr = " required" if required else ""
    return (
        f'<label><span class="form-label">{e(label)}</span><input class="form-control" name="{e(name)}" type="{e(kind)}" value="{e(value)}"'
        f"{required_attr} {options}></label>"
    )


def select(label, name, options, value=""):
    rows = "".join(
        f'<option value="{e(key)}"{" selected" if str(key) == str(value) else ""}>{e(title)}</option>'
        for key, title in options
    )
    return (
        f'<label><span class="form-label">{e(label)}</span>'
        f'<select class="form-select" name="{e(name)}">{rows}</select></label>'
    )


def hidden(**fields):
    return "".join(f'<input type="hidden" name="{e(k)}" value="{e(v)}">' for k, v in fields.items())


def form(path, contents, csrf, *, label="保存", fields=None, cls=""):
    classes = set(cls.split())
    style = "btn-outline-secondary line" if {"secondary", "logout"} & classes else "btn-primary"
    layout = f"{cls} d-grid gap-3" if "stack" in classes else cls
    return (
        f'<form method="post" class="{e(layout)}" action="{e(path)}">'
        f"{hidden(csrf=csrf, **(fields or {}))}{contents}"
        f'<button class="btn {style}">{e(label)}</button></form>'
    )


def get_form(path, contents, label="查询", cls=""):
    return (
        f'<form method="get" action="{e(path)}" class="filters d-flex flex-wrap align-items-end gap-3 mb-3 {e(cls)}">'
        f'{contents}<button class="btn btn-primary">{e(label)}</button></form>'
    )


def table(headers, rows):
    head = "".join(f"<th>{e(title)}</th>" for title in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    if not body:
        body = f'<tr><td colspan="{len(headers)}" class="empty">暂无符合条件的记录.</td></tr>'
    return (
        '<div class="card"><div class="table-responsive">'
        f'<table class="table table-vcenter card-table"><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div></div>"
    )


def badge(value):
    tone = {
        "待确认": "amber",
        "待协调": "amber",
        "待清洁": "amber",
        "维修中": "amber",
        "营业中": "green",
        "已确认": "green",
        "空闲": "green",
        "成功": "green",
        "已入住": "green",
        "已停业": "red",
        "已取消": "red",
        "失败": "red",
    }.get(value, "neutral")
    classes = {
        "amber": "bg-yellow-lt",
        "green": "bg-green-lt",
        "red": "bg-red-lt",
        "neutral": "bg-secondary-lt",
    }
    return f'<span class="badge {classes[tone]}">{e(value)}</span>'


def plate(title, contents):
    return (
        f'<section class="card panel"><div class="card-body">'
        f'<h2 class="card-title">{e(title)}</h2>{contents}</div></section>'
    )


STAFF_NAV = [
    ("前台", [("/orders", "订单工作台"), ("/coordination", "待协调订单")]),
    ("客房与售卖", [("/inventory", "库存与房价"), ("/rooms", "实体客房"), ("/types", "房型资料")]),
    ("经营", [("/stats", "业务统计")]),
]
ADMIN_NAV = [
    ("运营", [("/orders", "订单工作台"), ("/coordination", "待协调订单"), ("/stats", "业务统计")]),
    ("平台管理", [("/hotels", "酒店资料"), ("/grants", "人员授权"), ("/audit", "操作日志")]),
]


def workspace_nav(actor, path):
    section = "/orders" if path in ("/", "/order") else path
    groups = STAFF_NAV if actor.role == Role.STAFF else ADMIN_NAV
    navigation = []
    for label, links in groups:
        navigation.append(f'<li class="work-nav-group">{e(label)}</li>')
        for url, caption in links:
            active = " active" if url == section else ""
            current = ' aria-current="page"' if active else ""
            navigation.append(
                f'<li class="nav-item{active}"><a class="nav-link" href="{e(url)}"{current}>'
                f'<span class="nav-link-title">{e(caption)}</span></a></li>'
            )
    return "".join(navigation)


def guest_nav(actor, csrf, path):
    browse = '<a href="/#results">酒店与房型</a>'
    if actor:
        active = ' aria-current="page"' if path in ("/orders", "/order") else ""
        return f'{browse}<a href="/orders"{active}>我的订单</a>' + form(
            "/logout", "", csrf, label="退出", cls="logout"
        )
    current = ' aria-current="page"' if path == "/login" else ""
    return f'{browse}<a class="nav-account" href="/login"{current}>登录</a>'


def page(title, body, actor=None, csrf="", *, message="", landing=False, path=""):
    notice = (
        f'<p class="alert alert-warning notice" role="status">{e(message)}</p>' if message else ""
    )
    if actor and actor.role != Role.CUSTOMER:
        return template("workspace").substitute(
            title=e(title),
            navigation=workspace_nav(actor, path),
            role=ROLE_NAMES[actor.role],
            account=e(actor.name),
            logout=form("/logout", "", csrf, label="退出", cls="logout"),
            notice=notice,
            body=body,
        )
    return template("guest-layout").substitute(
        title=e(title),
        navigation=guest_nav(actor, csrf, path),
        class_name="" if landing else "wrap guest-page",
        heading="" if landing else f'<h1 class="page-title">{e(title)}</h1>',
        notice=notice,
        body=body,
    )


def login(csrf, next_url=""):
    inputs = field("账号", "username", autocomplete="username")
    inputs += field("密码", "password", kind="password", autocomplete="current-password")
    login_form = form("/login", inputs, csrf, label="登录", fields={"next": next_url}, cls="stack")
    return (
        '<section class="wrap login-layout">'
        f'<div class="login-media">{photo("login-room.jpg", "靠窗的酒店客房")}</div>'
        '<div class="login-panel"><h1>登录栖行</h1>'
        f'{login_form}<details class="demo-accounts"><summary>查看演示账号</summary>'
        "<p>顾客 <code>guest</code><br>酒店前台 <code>staff</code><br>"
        "平台管理员 <code>admin</code><br>统一密码 <code>qixing-demo</code></p>"
        "</details></div></section>"
    )


def stay_range(start, end):
    first, last = date_key(str(start)), date_key(str(end))
    if first.year != last.year:
        return f"{first.isoformat()} 至 {last.isoformat()}"
    return f"{first.month}月{first.day}日 – {last.month}月{last.day}日"


def search_form(query):
    primary = field("城市", "city", query["city"], required=False, placeholder="所有城市")
    primary += field("入住", "start", query["start"], "date")
    primary += field("退房", "end", query["end"], "date")
    primary += field("人数", "guests", query["guests"], "number", min=1, max=12)
    extra = field("房型关键词", "room", query["room"], required=False)
    extra += field("全程最低价", "min", query["min"], "number", required=False, min=0, step="0.01")
    extra += field("全程最高价", "max", query["max"], "number", required=False, min=0, step="0.01")
    open_attr = " open" if any(query[key] for key in ("room", "min", "max")) else ""
    return (
        '<form method="get" action="/#results" class="stay-search" aria-label="查找可订房型">'
        f'<div class="stay-search-main">{primary}<button class="btn btn-primary">查找房型 <span aria-hidden="true">→</span></button></div>'
        f'<details class="advanced"{open_attr}><summary>按房型和价格筛选</summary>'
        f'<div class="advanced-fields">{extra}</div></details></form>'
    )


def room_link(item, query):
    target = href(
        "/offer",
        room_type=item["id"],
        start=query["start"],
        end=query["end"],
        guests=query["guests"],
    )
    return (
        f'<a class="room-rate" href="{e(target)}">'
        f"<span><strong>{e(item['name'])}</strong>"
        f"<small>最多{item['max_guests']}人 · 可订{item['remaining']}间</small></span>"
        f'<span class="room-rate-price"><strong>¥{money(item["total"])}</strong>'
        '<span aria-hidden="true">↗</span></span></a>'
    )


def search(query, rows, *, filtered=False):
    properties = {}
    for row in rows:
        properties.setdefault(row["hotel_id"], []).append(row)
    results = []
    for rooms in properties.values():
        item = rooms[0]
        rates = "".join(room_link(room, query) for room in rooms)
        results.append(
            f'<article class="property"><div class="property-media">'
            f"{photo(item['photo'], item['hotel'], lazy=True)}</div>"
            f"<h3>{e(item['hotel'])}</h3><p class='property-address'>{e(item['address'])}</p>"
            f'<div class="property-rooms">{rates}</div></article>'
        )
    dates = stay_range(query["start"], query["end"])
    nights = (date_key(query["end"]) - date_key(query["start"])).days
    scope = f"{query['city']} · " if query["city"] else ""
    masthead = (
        ""
        if filtered
        else template("guest-home").substitute(
            photo=photo("hero-room.jpg", "木质墙面、藤编吊灯与临窗的大床")
        )
    )
    cards = '<div class="catalog-grid">' + "".join(results) + "</div>" if results else ""
    if not cards:
        reset = link("/#stay", "清除筛选", "button secondary")
        cards = (
            '<div class="empty-result"><h3>没有符合条件的房型</h3>'
            f"<p>试试调整日期、入住人数或价格范围.</p>{reset}</div>"
        )
    rooms = template("guest-rooms").substitute(
        heading="h1" if filtered else "h2",
        title="查询结果" if filtered else "可订酒店",
        summary=f"{e(scope)}{dates} · {nights}晚 · {e(query['guests'])}人 · 显示全程价",
        count=len(properties),
        cards=cards,
    )
    finder = f'<section class="booking-bar" id="stay"><div class="wrap">{search_form(query)}</div></section>'
    return masthead + finder + rooms


def stay_fields(stay, options=None):
    inputs = (
        select("房型", "room_type", options, stay.room_type)
        if options
        else hidden(room_type=stay.room_type)
    )
    inputs += field("入住人", "guest", stay.guest, maxlength=80)
    inputs += field("入住人数", "guests", stay.guests, "number", min=1, max=12)
    inputs += field("入住", "start", stay.arrival, "date")
    inputs += field("退房", "end", stay.departure, "date")
    return inputs


def offer(item, stay, prices, actor, csrf, key):
    dates = stay_range(stay.arrival, stay.departure)
    nights = len(prices)
    total = money(sum(price.unit_cents for price in prices))
    fields = {
        "room_type": stay.room_type,
        "start": stay.arrival,
        "end": stay.departure,
        "guests": stay.guests,
    }
    search_url = href(
        "/", city=item["city"], start=stay.arrival, end=stay.departure, guests=stay.guests
    )
    action = link(href("/login", next=href("/offer", **fields)), "登录后预订", "button")
    if actor and actor.role == Role.CUSTOMER:
        action = form(
            "/book",
            field("入住人", "guest", stay.guest, maxlength=80, autocomplete="name"),
            csrf,
            label="提交预订",
            fields={"key": key, **fields},
            cls="stack",
        )
    elif actor:
        action = '<p class="muted">预订需要顾客账号.</p>'
    breakdown = table(
        ["住宿日期", "当晚价格"],
        [(e(n.night), f"¥{money(n.unit_cents)}") for n in prices],
    )
    return (
        '<section class="wrap offer"><div class="offer-heading">'
        f"{link(search_url, '← 返回查询结果', 'back-link')}"
        f"<p>{e(item['hotel'])} · {e(item['address'])}</p>"
        f"<h1>{e(item['name'])}</h1></div>"
        '<div class="offer-layout"><article>'
        f'<div class="offer-media">{photo(item["photo"], item["hotel"])}</div>'
        f'<p class="offer-caption">最多入住 {item["max_guests"]} 人</p></article>'
        '<aside class="booking-sheet" aria-label="预订明细"><h2>本次住宿</h2>'
        f'<div class="booking-dates"><span>{dates} · {stay.guests}人</span>'
        f"{link(search_url + '#stay', '修改行程')}</div>"
        f'<div class="booking-total"><span>全程价格 · {nights}晚</span><strong>¥{total}</strong></div>'
        f'<details class="disclosure nightly-prices"><summary>查看逐晚房价</summary>{breakdown}</details>'
        f'{action}<p class="booking-note">提交时以酒店当前房价和余量为准. '
        "实际入住前可免费取消.</p></aside></div></section>"
    )


def duplicate(stay, result, csrf, key):
    fields = dict(
        room_type=stay.room_type,
        start=stay.arrival,
        end=stay.departure,
        guests=stay.guests,
        guest=stay.guest,
        key=key,
        challenge=result.challenge,
    )
    order = link(href("/order", id=result.matched_booking), f"订单 #{result.matched_booking}")
    return (
        '<section class="duplicate"><div class="duplicate-facts">'
        f'<span class="muted">已找到相同行程</span><strong>{order}</strong>'
        f"<span>{stay_range(stay.arrival, stay.departure)} · {stay.guests}人</span></div>"
        "<p>你可以保留现有订单, 或继续为这段行程另订一间房. "
        '继续提交时将重新检查余量和房价.</p><div class="actions">'
        + form("/book", "", csrf, label="继续另建订单", fields={**fields, "decision": "continue"})
        + form(
            "/book",
            "",
            csrf,
            label="放弃本次请求",
            fields={**fields, "decision": "decline"},
            cls="secondary",
        )
        + "</div></section>"
    )


def hotel_picker(hotels, current, label="酒店", include_all=False):
    options = [("", "全部")] if include_all else []
    return select(label, "hotel", options + [(h["id"], h["name"]) for h in hotels], current)


def order_filters(hotels, query, *, summary=False):
    inputs = hotel_picker(hotels, query.get("hotel", ""), include_all=True) if hotels else ""
    inputs += field("创建日从", "from", query.get("from", ""), "date", required=False)
    inputs += field("创建日至", "to", query.get("to", ""), "date", required=False)
    if not summary and query.get("state"):
        inputs += hidden(state=query["state"])
    return get_form("/stats" if summary else "/orders", inputs)


def order_status(row):
    return badge(row["state"]) + (badge("待协调") if row["coordinating"] else "")


def order_tabs(query):
    options = [("", "全部"), *[(state, state.value) for state in BookingState]]
    filters = {key: query[key] for key in ("hotel", "from", "to") if query.get(key)}
    links = []
    for value, title in options:
        target = href("/orders", **filters, **({"state": value} if value else {}))
        active = ' aria-current="true"' if query.get("state", "") == value else ""
        style = "nav-link active" if active else "nav-link"
        links.append(f'<a class="{style}" href="{e(target)}"{active}>{e(title)}</a>')
    return (
        '<nav class="order-tabs nav nav-tabs mb-3" aria-label="筛选订单状态">'
        + "".join(links)
        + "</nav>"
    )


def orders(rows, filters, actor, query):
    meta = f'<p class="records-count">显示 {len(rows)} 条订单</p>'
    if actor.role == Role.CUSTOMER:
        cards = []
        for row in rows:
            ident = row["id"]
            target = href("/order", id=ident)
            cards.append(
                f'<article class="trip-row">{photo(row["photo"], row["hotel"], lazy=True)}'
                f'<div><p class="muted">订单 #{ident}</p><h2>{link(target, row["hotel"])}</h2>'
                f"<p>{e(row['type_name'])} · {stay_range(row['arrival'], row['departure'])}</p></div>"
                f'<div class="trip-end">{order_status(row)}'
                f"<strong>¥{money(row['total'])}</strong>{link(target, '查看订单 ↗')}</div></article>"
            )
        results = "".join(cards) or (
            '<div class="empty-result"><h2>暂无符合条件的订单</h2>'
            f"{link('/', '预订住宿', 'button secondary')}</div>"
        )
    else:
        results = table(
            ["订单 / 入住人", "酒店 / 房型", "入住 → 退房", "状态", "全程金额", "操作"],
            [
                (
                    f"<strong>{e(row['guest'])}</strong><small class='d-block text-secondary'>#{row['id']}</small>",
                    f"{e(row['hotel'])}<small class='d-block text-secondary'>{e(row['type_name'])}</small>",
                    stay_range(row["arrival"], row["departure"]),
                    order_status(row),
                    f"¥{money(row['total'])}",
                    link(href("/order", id=row["id"]), "查看订单 ↗", "row-link"),
                )
                for row in rows
            ],
        )
    return order_tabs(query) + filters + meta + results


def facts_list(entries):
    lines = "".join(
        f'<div class="col"><dt class="text-secondary">{e(label)}</dt><dd class="mb-0">{e(value)}</dd></div>'
        for label, value in entries
    )
    return f'<dl class="fact-grid row row-cols-2 row-cols-lg-3 g-3 mb-0">{lines}</dl>'


def disclosure(title, body):
    return (
        f'<details class="disclosure card card-body"><summary>{e(title)}</summary>{body}</details>'
    )


def order_actions(row, actor, csrf, candidates, types):
    ident = {"id": row["id"]}
    if actor.role == Role.CUSTOMER and row["state"] in (
        BookingState.PENDING,
        BookingState.CONFIRMED,
    ):
        stay = Stay(
            row["room_type"],
            date_key(row["arrival"]),
            date_key(row["departure"]),
            row["guests"],
            row["guest"],
        )
        modify = form(
            "/modify", stay_fields(stay, types), csrf, label="保存修改", fields=ident, cls="stack"
        )
        cancel = "<p>实际入住前可免费取消. 取消后将释放该订单的预订库存.</p>" + form(
            "/cancel", "", csrf, label="确认取消", fields=ident, cls="secondary"
        )
        return plate(
            "管理预订", disclosure("调整入住安排", modify) + disclosure("取消预订", cancel)
        )
    if actor.role != Role.STAFF:
        return ""
    if row["state"] == BookingState.PENDING:
        return plate(
            "确认订单",
            "<p>核对入住信息后确认, 顾客可在订单中查看结果.</p>"
            + form("/confirm", "", csrf, label="酒店确认", fields=ident),
        )
    if row["state"] == BookingState.CONFIRMED:
        rooms = [(r["id"], r["number"]) for r in candidates]
        return plate(
            "办理入住",
            form(
                "/check-in",
                select("空闲房号", "room", rooms),
                csrf,
                label="分配并入住",
                fields=ident,
                cls="stack",
            )
            if rooms
            else "<p>当前没有可分配客房. 请刷新页面或记录协调结果.</p>",
        )
    if row["state"] == BookingState.IN_HOUSE:
        return plate(
            "办理退房",
            "<p>退房完成后, 已分配房间会转为待清洁.</p>"
            + form("/check-out", "", csrf, label="确认退房", fields=ident),
        )
    return ""


def detail(data, actor, csrf, candidates=(), types=()):
    row, revisions, nightly, assignments = data
    prices = table(
        ["版本", "住宿日", "当晚金额"],
        [(str(n["version"]), e(n["night"]), money(n["price"])) for n in nightly],
    )
    history = table(
        ["版本", "房型", "日期", "入住人", "人数", "总价", "修订时间"],
        [
            (
                str(n["version"]),
                e(n["type_name"]),
                stay_range(n["arrival"], n["departure"]),
                e(n["guest"]),
                str(n["guests"]),
                money(n["total"]),
                display_time(n["changed"]),
            )
            for n in revisions
        ],
    )
    details = facts_list(
        [
            ("入住人", row["guest"]),
            ("入住人数", f"{row['guests']}人"),
            ("入住日期", row["arrival"]),
            ("退房日期", row["departure"]),
            ("房型", row["type_name"]),
            ("首次下单 (UTC+8)", display_time(row["created"])),
        ]
    )
    allocation = ""
    if assignments:
        allocation = plate(
            "入住分配",
            table(
                ["房号", "实际入住", "实际退房"],
                [
                    (e(a["number"]), display_time(a["started"]), display_time(a["ended"]))
                    for a in assignments
                ],
            ),
        )
    status_details = f"<p>{order_status(row)}</p>"
    if row["state"] == BookingState.PENDING:
        status_details += (
            '<p class="muted">本轮确认截止 (UTC+8)</p>'
            f'<p class="deadline">{display_time(row["waiting_since"] + CONFIRM_WINDOW)}</p>'
            f'<p class="muted">本轮起点 {display_time(row["waiting_since"])}</p>'
        )
    if row["cancel_reason"]:
        status_details += f"<p>{e(row['cancel_reason'])} · 取消费用 ¥{money(row['cancel_fee'])}</p>"
    status_details += f'<div class="order-total d-flex align-items-center justify-content-between"><span>全程金额</span><strong class="h2 mb-0">¥{money(row["total"])}</strong></div>'
    actions = order_actions(row, actor, csrf, candidates, types)
    hero = (
        f'<figure class="trip-photo">{photo(row["photo"], row["hotel"])}</figure>'
        if actor.role == Role.CUSTOMER
        else ""
    )
    return (
        f"{link('/orders', '← 返回订单列表', 'back-link')}"
        '<div class="reservation-layout row g-3"><div class="reservation-main col-xl-8 d-grid gap-3 align-content-start">'
        f"{hero}{plate(row['hotel'], details)}{allocation}"
        f"{disclosure('历次修订', history)}{disclosure('逐晚价格快照', prices)}</div>"
        f'<aside class="reservation-aside col-xl-4 d-grid gap-3 align-content-start">{plate("订单状态", status_details)}'
        f"{actions}</aside></div>"
    )


def desk_split(content, editor):
    if not editor:
        return content
    return f'<div class="work-split"><div>{content}</div><aside>{editor}</aside></div>'


def inventory_page(hotels, selected, types, rows, first, csrf, edit_type=None, edit_night=""):
    options = [(r["id"], r["name"]) for r in types]
    filters = hotel_picker(hotels, selected) + field("日期从", "first", first, "date")
    active = next(
        (row for row in rows if row["room_type"] == edit_type and row["night"] == edit_night),
        None,
    )
    edit = (
        select("房型", "room_type", options, active["room_type"] if active else "")
        + field("住宿日", "night", active["night"] if active else first, "date")
        + field("库存总量", "total", active["total"] if active else "", "number", min=0)
        + field(
            "当晚房价",
            "price",
            money(active["price"]) if active else "",
            "number",
            min=0,
            step="0.01",
        )
        + select(
            "售卖状态", "stopped", [(0, "出售"), (1, "停售")], active["stopped"] if active else 0
        )
    )
    entries = []
    for row in rows:
        remaining = max(0, row["total"] - row["occupied"] - row["repairs"])
        status = "停售" if row["stopped"] else str(remaining)
        target = href(
            "/inventory", hotel=selected, first=first, edit=row["room_type"], night=row["night"]
        )
        entries.append(
            (
                e(row["night"]),
                e(row["name"]),
                str(row["total"]),
                str(row["occupied"]),
                str(row["repairs"]),
                e(status),
                f"¥{money(row['price'])}",
                link(target, "编辑", "row-link"),
            )
        )
    rows_table = table(
        ["住宿日", "房型", "总量", "占用", "维修", "可售", "房价", "操作"],
        entries,
    )
    editor = plate("编辑每日售卖", form("/stock", edit, csrf, cls="stack")) if options else ""
    return get_form("/inventory", filters) + desk_split(rows_table, editor)


def rooms_page(hotels, selected, rows, types, today, csrf):
    entries = []
    for room in rows:
        ident = {"id": room["id"]}
        actions = "—"
        repair = "—"
        if room["state"] == RoomState.DIRTY:
            actions = form("/clean", "", csrf, label="确认清洁完成", fields=ident, cls="secondary")
        elif room["state"] == RoomState.FREE:
            fields = field("受影响首晚", "first", today, "date")
            fields += field("受影响末晚", "last", today, "date")
            fields += field("原因", "reason", maxlength=100)
            actions = disclosure(
                "登记维修",
                form("/repair", fields, csrf, label="开始维修", fields=ident, cls="stack"),
            )
        elif room["state"] == RoomState.REPAIR:
            repair = f"{e(room['reason'])}<small>至 {e(room['last_night'])}</small>"
            actions = form(
                "/restore", "", csrf, label="确认恢复可用", fields=ident, cls="secondary"
            )
        entries.append(
            (
                f"<strong>{e(room['number'])}</strong>",
                e(room["type_name"]),
                badge(room["state"]),
                repair,
                actions,
            )
        )
    numbers = {state: sum(room["state"] == state for room in rows) for state in RoomState}
    overview = (
        '<dl class="room-overview">'
        + "".join(
            f"<div><dt>{e(state)}</dt><dd>{count}</dd></div>" for state, count in numbers.items()
        )
        + "</dl>"
    )
    options = [(r["id"], r["name"]) for r in types]
    add = select("房型", "room_type", options) + field("房号", "number", maxlength=20)
    editor = plate("登记客房", form("/room-save", add, csrf, cls="stack")) if options else ""
    rooms = table(["房号", "房型", "状态", "维修说明", "操作"], entries)
    return get_form("/rooms", hotel_picker(hotels, selected)) + overview + desk_split(rooms, editor)


def type_form(item, hotel, csrf):
    fields = field("房型名称", "name", item["name"], maxlength=80)
    fields += field("最多入住人数", "max_guests", item["max_guests"], "number", min=1, max=12)
    return form("/type-save", fields, csrf, fields={"hotel": hotel, "id": item["id"]})


def types_page(hotels, selected, rows, csrf):
    entries = [
        (
            e(item["name"]),
            str(item["max_guests"]),
            disclosure("编辑房型", type_form(item, selected, csrf)),
        )
        for item in rows
    ]
    new = {"id": "", "name": "", "max_guests": 2}
    editor = plate("新增房型", type_form(new, selected, csrf))
    return get_form("/types", hotel_picker(hotels, selected)) + desk_split(
        table(["房型名称", "最多入住人数", "操作"], entries),
        editor,
    )


def hotel_form(item, csrf):
    inputs = "".join(
        field(label, key, item[key], maxlength=100)
        for key, label in (("name", "名称"), ("city", "城市"), ("address", "地址"))
    )
    return form("/hotel-save", inputs, csrf, fields={"id": item["id"]}, cls="stack")


def hotels_page(rows, csrf):
    entries = []
    for item in rows:
        verb = "恢复营业" if item["closed"] else "临时停业"
        update = form(
            "/hotel-status",
            field("原因", "reason", maxlength=100),
            csrf,
            label=verb,
            fields={"id": item["id"], "closed": int(not item["closed"])},
            cls="secondary stack",
        )
        edit = disclosure("编辑资料", hotel_form(item, csrf)) + disclosure(verb, update)
        entries.append(
            (
                f"<strong>{e(item['name'])}</strong><small>{e(item['address'])}</small>",
                e(item["city"]),
                badge("已停业" if item["closed"] else "营业中"),
                edit,
            )
        )
    new = dict(id="", name="", city="", address="")
    return desk_split(
        table(["酒店", "城市", "营业状态", "操作"], entries),
        plate("新增酒店", hotel_form(new, csrf)),
    )


def grants_page(rows, hotels, csrf):
    names = {r["id"]: r["name"] for r in rows}
    inputs = select("工作人员", "user", names.items())
    inputs += hotel_picker(hotels, "")
    inputs += select("操作", "allowed", [(1, "授予"), (0, "收回")])
    listing = table(
        ["工作人员", "账号", "授权酒店"],
        [(e(r["name"]), e(r["username"]), e(r["hotel"] or "—")) for r in rows],
    )
    return desk_split(listing, plate("调整酒店授权", form("/grant", inputs, csrf, cls="stack")))


def coordination_page(rows, csrf):
    return table(
        ["酒店", "订单", "当前状态", "协调原因", "处理结果"],
        [
            (
                e(r["hotel"]),
                link(href("/order", id=r["booking_id"]), f"#{r['booking_id']}"),
                badge(r["state"]),
                e(r["reason"]),
                e(r["resolution"])
                if r["resolution"]
                else form(
                    "/resolve",
                    field("处理说明", "resolution", maxlength=100),
                    csrf,
                    label="完成协调",
                    fields={"id": r["id"]},
                ),
            )
            for r in rows
        ],
    )


def stats(totals, breakdown, filters):
    cards = "".join(
        '<div class="col">' + plate(label, f'<strong class="h2">{value}</strong>') + "</div>"
        for label, value in [
            ("订单总数", totals["count"]),
            ("当前取消", totals["cancelled"]),
            ("已完成金额", f"¥{money(totals['revenue'])}"),
        ]
    )
    return (
        filters
        + '<p class="text-secondary">按首次创建日选取同一批次, 日期采用 UTC+8.</p>'
        + '<div class="work-metrics">'
        + cards
        + "</div>"
        + table(["当前状态", "订单数量"], [(badge(r["state"]), str(r["count"])) for r in breakdown])
    )


def audit(rows, query):
    inputs = field("对象", "target", query.get("target", ""), required=False)
    inputs += field("操作", "action", query.get("action", ""), required=False)
    inputs += select(
        "结果",
        "outcome",
        [("", "全部"), ("成功", "成功"), ("失败", "失败")],
        query.get("outcome", ""),
    )
    inputs += field("日期从", "from", query.get("from", ""), "date", required=False)
    inputs += field("日期至", "to", query.get("to", ""), "date", required=False)
    return get_form("/audit", inputs) + table(
        ["时间 (UTC+8)", "人员", "操作 / 对象", "结果", "细节"],
        [
            (
                display_time(r["occurred"]),
                e(r["actor"]),
                e(f"{r['action']} / {r['target']}"),
                badge(r["outcome"]),
                f"<details><summary>{e(r['detail'] or '变更内容')}</summary>"
                f"<p>操作编号 {e(r['operation_id'])}</p>"
                f"<pre class='text-wrap text-break'>之前: {e(r['before_json'])}\n之后: {e(r['after_json'])}</pre></details>",
            )
            for r in rows
        ],
    )
