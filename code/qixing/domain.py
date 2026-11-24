"""Domain rules independent of HTTP and the database."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from time import time_ns

LOCAL = timezone(timedelta(hours=8))
CONFIRM_WINDOW = 30 * 60 * 1_000_000


class BusinessError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class Role(StrEnum):
    CUSTOMER = "customer"
    STAFF = "staff"
    ADMIN = "admin"


class BookingState(StrEnum):
    PENDING = "待确认"
    CONFIRMED = "已确认"
    IN_HOUSE = "已入住"
    COMPLETED = "已完成"
    CANCELLED = "已取消"


class RoomState(StrEnum):
    FREE = "空闲"
    OCCUPIED = "已入住"
    DIRTY = "待清洁"
    REPAIR = "维修中"


class RequestState(StrEnum):
    OPEN = "OPEN"
    WAITING = "AWAITING_CONSENT"
    SUCCEEDED = "SUCCEEDED"
    DECLINED = "DECLINED"


def now_us() -> int:
    return time_ns() // 1_000


def local_date() -> date:
    return datetime.now(LOCAL).date()


def display_time(value: int | None) -> str:
    if value is None:
        return "—"
    return datetime.fromtimestamp(value / 1_000_000, LOCAL).strftime("%Y-%m-%d %H:%M:%S")


def date_key(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise BusinessError("请输入有效日期.", 400) from error
    if parsed.isoformat() != value:
        raise BusinessError("日期格式应为 YYYY-MM-DD.", 400)
    return parsed


def day_bounds(first: str, last: str) -> tuple[int, int]:
    start = date_key(first)
    end = date_key(last)
    if end < start or end == date.max:
        raise BusinessError("日期范围无效.", 400)
    return tuple(
        int(datetime.combine(day, datetime.min.time(), LOCAL).timestamp()) * 1_000_000
        for day in (start, end + timedelta(days=1))
    )


def required(value: str, label: str, limit: int = 100) -> str:
    value = value.strip()
    if not value or len(value) > limit:
        raise BusinessError(f"{label}不能为空, 且不得超过 {limit} 个字符.", 400)
    return value


def integer(value: str, label: str, minimum: int = 1, maximum: int = 1_000_000) -> int:
    try:
        number = int(value)
    except (ValueError, TypeError) as error:
        raise BusinessError(f"{label}应为整数.", 400) from error
    if not minimum <= number <= maximum:
        raise BusinessError(f"{label}应在 {minimum} 至 {maximum} 之间.", 400)
    return number


def cents(value: str) -> int:
    try:
        amount = Decimal(value)
        if not amount.is_finite() or not 0 <= amount <= 1_000_000:
            raise InvalidOperation
        scaled = amount * 100
        if scaled != scaled.to_integral_value():
            raise InvalidOperation
    except (InvalidOperation, ValueError) as error:
        raise BusinessError("金额须为有效非负数, 最多保留两位小数.", 400) from error
    return int(scaled)


def money(value: int) -> str:
    whole, fraction = divmod(abs(value), 100)
    sign = "-" if value < 0 else ""
    return f"{sign}{whole}.{fraction:02d}"


@dataclass(frozen=True)
class Stay:
    room_type: int
    arrival: date
    departure: date
    guests: int
    guest: str

    def __post_init__(self):
        if not 1 <= (self.departure - self.arrival).days <= 30:
            raise BusinessError("住宿区间须为 1 至 30 晚.", 400)
        if not 1 <= self.guests <= 12:
            raise BusinessError("入住人数应在 1 至 12 之间.", 400)
        required(self.guest, "入住人", 80)

    @property
    def nights(self) -> tuple[str, ...]:
        return tuple(
            (self.arrival + timedelta(days=offset)).isoformat()
            for offset in range((self.departure - self.arrival).days)
        )

    @property
    def itinerary(self) -> tuple[int, str, str]:
        return self.room_type, self.arrival.isoformat(), self.departure.isoformat()

    def fingerprint(self) -> list:
        return [*self.itinerary, self.guests, self.guest]


@dataclass(frozen=True)
class NightPrice:
    night: str
    unit_cents: int


@dataclass(frozen=True)
class BookingRevision:
    version: int
    stay: Stay
    prices: tuple[NightPrice, ...]

    @property
    def total(self) -> int:
        return sum(price.unit_cents for price in self.prices)


@dataclass(frozen=True)
class Booking:
    id: int
    customer: int
    hotel: int
    state: BookingState
    revision: BookingRevision
    waiting_since: int

    @property
    def deadline(self) -> int:
        return self.waiting_since + CONFIRM_WINDOW

    def expired(self, instant: int) -> bool:
        return self.state == BookingState.PENDING and instant > self.deadline

    def require(self, *states: BookingState) -> None:
        if self.state not in states:
            raise BusinessError(f"订单当前为{self.state}, 无法执行此操作.")


@dataclass(frozen=True)
class DailyInventory:
    total: int
    occupied: int
    repairs: int
    price: int
    stopped: bool

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.repairs - self.occupied)

    def check_sale(self, delta: int) -> None:
        if delta < 0:
            if self.occupied + delta < 0:
                raise BusinessError("逐晚占用不一致.")
            return
        if self.stopped:
            raise BusinessError("所选行程包含停售日期.")
        if delta > self.remaining:
            raise BusinessError("所选行程至少有一晚余量不足.")


@dataclass(frozen=True)
class Actor:
    id: int
    name: str
    role: Role


@dataclass(frozen=True)
class Submission:
    state: RequestState
    booking_id: int | None = None
    challenge: str | None = None
    matched_booking: int | None = None
