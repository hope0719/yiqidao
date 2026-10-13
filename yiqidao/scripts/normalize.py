"""数据规范化：把 flyai-cli 的原始 JSON 转成内部统一结构。

实测（2026-10-10）字段差异，本模块据此编写，不采信文档：

  * 机票 item 用 `ticketPrice`（如 "750.00"），火车 item 用 `price`（如 "23.50"）。
    官方文档写的是 `adultPrice`，实测该键为 None。故按候选列表逐个探测。
  * `duration` / `totalDuration` 实测是纯数字字符串（分钟，如 "185"），
    而文档示例写的是 "140分钟"。两种格式都要能解析。
  * 酒店 `price` 是搜索展示价，可能为床位或促销起价；不能据此认定整房每晚价格。
  * 火车 `quantity`（余票）实测恒为 null，机票无余票字段。
    因此任何库存结论都不可下，统一标记为未核实。
  * 酒店可能缺 `score`/`scoreDesc`/`review`（实测只有 `rate`）。
  * POI 的 `ticketInfo` 可能为 null，`price` 形如 "¥68"。

价格缺失一律保留 None，绝不用 0 顶替——0 会被下游误当成"免费"，
从而错误地宣称满足预算。
"""

from __future__ import annotations

import re
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

TRANSPORT_PRICE_KEYS = ("ticketPrice", "adultPrice", "price", "totalPrice")
LODGING_PRICE_KEYS = ("price", "adultPrice")

_CURRENCY_RE = re.compile(r"[¥￥$\s,]")
_DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def parse_money(raw: Any) -> Optional[float]:
    """把 "¥618" / "750.00" / 618 解析为 float；无法解析返回 None。

    返回 None 而非 0 是本项目的硬规则：缺失价格不得参与加总，
    否则会把"不知道多少钱"变成"免费"。
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if not isinstance(raw, bool) and math.isfinite(raw) and raw >= 0 else None
    if not isinstance(raw, str):
        return None
    if "$" in raw or re.search(r"USD|EUR|美元|欧元", raw, re.I):
        return None
    text = _CURRENCY_RE.sub("", raw.strip())
    if not text:
        return None
    match = re.fullmatch(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        amount = float(match.group())
        return amount if math.isfinite(amount) and amount >= 0 else None
    except ValueError:
        return None


def parse_minutes(raw: Any) -> Optional[int]:
    """把 "185" / "140分钟" / "3小时20分" 解析为分钟数。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw) if not isinstance(raw, bool) and math.isfinite(raw) and raw >= 0 else None
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None

    hours = re.search(r"(\d+)\s*(?:小时|h|H)", text)
    mins = re.search(r"(\d+)\s*(?:分钟|分|m|M)(?!onth)", text)
    if hours or mins:
        total = 0
        if hours:
            total += int(hours.group(1)) * 60
        if mins:
            total += int(mins.group(1))
        return total if total else None

    match = re.search(r"^-?\d+(?:\.\d+)?$", text)
    if match:
        number = float(text)
        return int(number) if math.isfinite(number) and number >= 0 else None
    return None


def parse_datetime(raw: Any) -> Optional[datetime]:
    """解析 "2026-10-20 06:55:00" 形式的时间。"""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def first_present(item: Dict[str, Any], keys: tuple) -> Any:
    """返回 keys 中第一个非空值。"""
    for key in keys:
        val = item.get(key)
        if val not in (None, "", []):
            return val
    return None


@dataclass
class Segment:
    """一段不可分割的行程。"""

    transport_no: Optional[str]
    transport_name: Optional[str]
    transport_type: Optional[str]
    dep_city: Optional[str]
    dep_station: Optional[str]
    dep_time: Optional[datetime]
    arr_city: Optional[str]
    arr_station: Optional[str]
    arr_time: Optional[datetime]
    duration_min: Optional[int]
    seat_class: Optional[str]
    stops: Optional[Any] = None

    @classmethod
    def from_raw(cls, seg: Dict[str, Any]) -> "Segment":
        return cls(
            transport_no=seg.get("marketingTransportNo"),
            transport_name=seg.get("marketingTransportName"),
            transport_type=seg.get("transportType"),
            dep_city=seg.get("depCityName"),
            dep_station=seg.get("depStationName") or seg.get("depStationShortName"),
            dep_time=parse_datetime(seg.get("depDateTime")),
            arr_city=seg.get("arrCityName"),
            arr_station=seg.get("arrStationName") or seg.get("arrStationShortName"),
            arr_time=parse_datetime(seg.get("arrDateTime")),
            duration_min=parse_minutes(seg.get("duration")),
            seat_class=seg.get("seatClassName"),
            stops=seg.get("stopInfos"),
        )


@dataclass
class TransportOption:
    """一个单程交通候选（去程或返程）。"""

    mode: str  # flight | train
    direction: str  # outbound | return
    price: Optional[float]
    price_is_missing: bool
    total_duration_min: Optional[int]
    journey_type: Optional[str]
    segments: List[Segment] = field(default_factory=list)
    jump_url: Optional[str] = None
    transfer_duration_min: Optional[int] = None
    seats_remaining: Optional[Any] = None  # 实测恒为 None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def dep_time(self) -> Optional[datetime]:
        return self.segments[0].dep_time if self.segments else None

    @property
    def arr_time(self) -> Optional[datetime]:
        return self.segments[-1].arr_time if self.segments else None

    @property
    def dep_station(self) -> Optional[str]:
        return self.segments[0].dep_station if self.segments else None

    @property
    def arr_station(self) -> Optional[str]:
        return self.segments[-1].arr_station if self.segments else None

    @property
    def transport_summary(self) -> str:
        """如 "FM9537 上航 直达" 或 "D352->K457 中转"。"""
        nos = [s.transport_no or "?" for s in self.segments]
        label = "->".join(nos)
        names = {s.transport_name for s in self.segments if s.transport_name}
        carrier = "/".join(sorted(names))
        parts = [label]
        if carrier:
            parts.append(carrier)
        if self.journey_type:
            parts.append(self.journey_type)
        return " ".join(parts)

    @classmethod
    def from_raw(
        cls, item: Dict[str, Any], mode: str, direction: str
    ) -> "TransportOption":
        journeys = item.get("journeys") or []
        if not isinstance(journeys, list):
            journeys = []
        segs_raw: List[Dict[str, Any]] = []
        journey_type = None
        transfer_dur = None
        if journeys and isinstance(journeys[0], dict):
            first = journeys[0]
            segs_raw = first.get("segments") or []
            if not isinstance(segs_raw, list):
                segs_raw = []
            if any(not isinstance(seg, dict) for seg in segs_raw):
                segs_raw = []
            journey_type = first.get("journeyType")
            transfer_dur = parse_minutes(first.get("transferDuration"))

        raw_price = first_present(item, TRANSPORT_PRICE_KEYS)
        price = parse_money(raw_price)

        seats = None
        for s in segs_raw:
            if s.get("quantity") is not None:
                seats = s.get("quantity")
                break

        return cls(
            mode=mode,
            direction=direction,
            price=price,
            price_is_missing=price is None,
            total_duration_min=parse_minutes(item.get("totalDuration")),
            journey_type=journey_type,
            segments=[Segment.from_raw(s) for s in segs_raw],
            jump_url=item.get("jumpUrl"),
            transfer_duration_min=transfer_dur,
            seats_remaining=seats,
            raw=item,
        )


def normalize_transport(
    items: List[Dict[str, Any]], mode: str, direction: str
) -> List[TransportOption]:
    """批量规范化交通结果，丢弃无法解析出时刻的条目。"""
    out: List[TransportOption] = []
    for item in items:
        opt = TransportOption.from_raw(item, mode, direction)
        # 没有可用时刻的条目无法参与共同停留时间计算，直接丢弃
        if not opt.segments or opt.dep_time is None or opt.arr_time is None:
            continue
        if any(seg.dep_time is None or seg.arr_time is None or seg.arr_time <= seg.dep_time for seg in opt.segments):
            continue
        if any(a.arr_time > b.dep_time for a, b in zip(opt.segments, opt.segments[1:])):
            continue
        if opt.arr_time <= opt.dep_time:
            continue
        clock_minutes = int((opt.arr_time - opt.dep_time).total_seconds() / 60)
        opt.total_duration_min = max(opt.total_duration_min or 0, clock_minutes)
        out.append(opt)
    return out


@dataclass
class HotelOption:
    """搜索展示价与已确认整房每晚报价严格区分。"""

    name: str
    price_per_night: Optional[float]
    price_is_missing: bool
    star: Optional[str]
    rate: Optional[float]
    address: Optional[str]
    nearby: Optional[str]
    detail_url: Optional[str]
    main_pic: Optional[str]
    hotel_id: Optional[str]
    latitude: Optional[str] = None
    longitude: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)
    display_price: Optional[float] = None
    quote_confirmed: bool = False
    max_guests_per_room: Optional[int] = None
    quote_source: Optional[str] = None

    @classmethod
    def from_confirmed_quote(cls, quote: Dict[str, Any]) -> "HotelOption":
        return cls(
            name=quote["hotel_name"], price_per_night=float(quote["room_price_per_night"]),
            price_is_missing=False, star=None, rate=None, address=None, nearby=None,
            detail_url=quote.get("detail_url"), main_pic=None, hotel_id=None,
            quote_confirmed=True, max_guests_per_room=quote["max_guests_per_room"],
            quote_source=quote["source"],
        )

    @classmethod
    def from_raw(cls, item: Dict[str, Any]) -> "HotelOption":
        price = parse_money(first_present(item, LODGING_PRICE_KEYS))
        rate = parse_money(item.get("rate") or item.get("score"))
        return cls(
            name=item.get("name") or "(未命名酒店)",
            price_per_night=None,
            price_is_missing=True,
            display_price=price,
            star=item.get("star"),
            rate=rate,
            address=item.get("address"),
            nearby=item.get("interestsPoi"),
            detail_url=item.get("detailUrl") or item.get("jumpUrl"),
            main_pic=item.get("mainPic"),
            hotel_id=item.get("shId"),
            latitude=item.get("latitude"),
            longitude=item.get("longitude"),
            raw=item,
        )


def normalize_hotels(items: List[Dict[str, Any]]) -> List[HotelOption]:
    return [HotelOption.from_raw(i) for i in items if i.get("name")]


@dataclass
class PoiOption:
    """一个景点候选。"""

    name: str
    category: Optional[str]
    description: Optional[str]
    free_status: Optional[str]
    ticket_price: Optional[float]
    ticket_name: Optional[str]
    address: Optional[str]
    jump_url: Optional[str]
    main_pic: Optional[str]
    rank: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, item: Dict[str, Any]) -> "PoiOption":
        ti = item.get("ticketInfo") or {}
        if not isinstance(ti, dict):
            ti = {}
        return cls(
            name=item.get("name") or "(未命名景点)",
            category=item.get("category"),
            description=item.get("description"),
            free_status=item.get("freePoiStatus"),
            ticket_price=parse_money(ti.get("price")),
            ticket_name=ti.get("ticketName"),
            address=item.get("address"),
            jump_url=item.get("jumpUrl"),
            main_pic=item.get("mainPic"),
            rank=item.get("listRank"),
            raw=item,
        )


def normalize_pois(items: List[Dict[str, Any]]) -> List[PoiOption]:
    return [PoiOption.from_raw(i) for i in items if i.get("name")]
