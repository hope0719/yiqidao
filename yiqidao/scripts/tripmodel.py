"""行程数据模型与硬性条件校验。

这里只放"能由代码判定对错"的逻辑：日期区间是否自洽、预算是否为正、
住宿分摊是否明确。所有比较、加总、排序都在代码里完成，不交给模型心算。

设计上刻意区分三组容易混淆的概念（需求文档明确要求）：
  * 每人预算 vs 团队总预算
  * 单程时长上限 vs 往返总时长上限
  * 可出发时间窗 vs 必须回到出发城市的时间

住宿分摊必须显式给出。人数/房间数不明确时，validate_request() 会产出
"必须补问"的问题，绝不默认"一间房能住所有人"。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

MAX_MEMBERS = 4
MIN_MEMBERS = 2
MAX_CANDIDATES = 5
MAX_NIGHTS = 3  # 首版只做 2-3 天短途

MODE_FLIGHT = "flight"
MODE_TRAIN = "train"
VALID_MODES = {MODE_FLIGHT, MODE_TRAIN}


@dataclass
class Member:
    """一位出行成员的条件。

    dep_* 是可出发时间窗；return_* 是返程时间窗；
    back_home_by 是"必须回到出发城市"的硬截止时间（作用于返程到达时刻）。
    """

    name: str
    origin_city: str
    dep_date_start: Optional[date] = None
    dep_date_end: Optional[date] = None
    ret_date_start: Optional[date] = None
    ret_date_end: Optional[date] = None
    back_home_by: Optional[datetime] = None
    budget: Optional[float] = None  # 每人预算，单位 CNY
    max_oneway_minutes: Optional[int] = None  # 单程上限
    max_roundtrip_minutes: Optional[int] = None  # 往返合计上限
    modes: List[str] = field(default_factory=lambda: [MODE_FLIGHT, MODE_TRAIN])
    notes: Optional[str] = None

    def transport_window(self) -> Dict[str, Any]:
        return {
            "dep_date_start": self.dep_date_start,
            "dep_date_end": self.dep_date_end,
            "ret_date_start": self.ret_date_start,
            "ret_date_end": self.ret_date_end,
        }


@dataclass
class RoomingPlan:
    """住宿方案：几间房、住几晚、谁和谁一间。"""

    rooms: int
    nights: int
    assignments: Optional[Dict[str, str]] = None  # member_name -> room_label
    note: Optional[str] = None

    @property
    def room_labels(self) -> List[str]:
        return [f"R{i + 1}" for i in range(self.rooms)]

    def occupancy(self) -> Dict[str, List[str]]:
        """按房间聚合成员；未分配时返回空 dict。"""
        if not self.assignments:
            return {}
        out: Dict[str, List[str]] = {r: [] for r in self.room_labels}
        for member, room in self.assignments.items():
            out.setdefault(room, []).append(member)
        return out


@dataclass
class TripRequest:
    """一次完整的会合请求。"""

    members: List[Member]
    candidates: List[str] = field(default_factory=list)
    team_budget: Optional[float] = None
    rooming: Optional[RoomingPlan] = None
    nights: Optional[int] = None
    min_common_hours: float = 12.0
    today: Optional[date] = None
    hotel_poi: Optional[str] = None  # 可选：酒店定位参考点，需用户显式给出
    hotel_quote: Optional[Dict[str, Any]] = None  # 用户明确确认的整房报价，不能由模型编造
    arrival_buffer_minutes: int = 60
    departure_buffer_minutes: int = 90
    home_buffer_minutes: int = 60

    @property
    def n_members(self) -> int:
        return len(self.members)

    def member(self, name: str) -> Optional[Member]:
        for m in self.members:
            if m.name == name:
                return m
        return None


# --------------------------------------------------------------------------
# 请求级校验
# --------------------------------------------------------------------------

@dataclass
class ValidationIssue:
    """一条校验问题。blocking=True 表示必须解决才能继续。"""

    field: str
    message: str
    blocking: bool = True
    question: Optional[str] = None  # 需要向用户补问的话术

    def to_dict(self) -> Dict[str, Any]:
        return {
            "field": self.field,
            "message": self.message,
            "blocking": self.blocking,
            "question": self.question,
        }


def _validate_member(m: Member, today: date) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    tag = f"members.{m.name}"

    if not m.name or not m.name.strip():
        issues.append(ValidationIssue(tag, "成员缺少名称"))
    if not m.origin_city or not m.origin_city.strip():
        issues.append(ValidationIssue(
            tag, f"成员 {m.name} 缺少出发城市",
            question=f"{m.name} 从哪个城市出发？"))

    # 预算：必须为正数；负数或 0 都是不合理的
    if m.budget is not None and m.budget <= 0:
        issues.append(ValidationIssue(
            tag, f"{m.name} 的预算必须为正数，当前 {m.budget}"))

    # 时长上限：单程与往返分别校验，往返不得小于单程
    if m.max_oneway_minutes is not None and m.max_oneway_minutes <= 0:
        issues.append(ValidationIssue(tag, f"{m.name} 的单程时长上限必须为正"))
    if m.max_roundtrip_minutes is not None and m.max_roundtrip_minutes <= 0:
        issues.append(ValidationIssue(tag, f"{m.name} 的往返时长上限必须为正"))
    # 两个数都是上限，往返上限更小也可能由短程班次满足，不能误判矛盾。
    if (m.max_oneway_minutes and m.max_roundtrip_minutes
            and m.max_roundtrip_minutes < m.max_oneway_minutes):
        issues.append(ValidationIssue(tag, "往返上限比单程上限更紧，将同时校验实际往返和单程时长", blocking=False))

    # 交通方式
    for mode in m.modes:
        if mode not in VALID_MODES:
            issues.append(ValidationIssue(
                tag, f"{m.name} 的交通方式 {mode!r} 不支持，可用：{sorted(VALID_MODES)}"))
    if not m.modes:
        issues.append(ValidationIssue(
            tag, f"{m.name} 未指定任何交通方式",
            question=f"{m.name} 接受飞机、火车，还是两者都可以？"))

    if m.dep_date_start is None or m.dep_date_end is None:
        issues.append(ValidationIssue(tag, "出发日期窗不完整", question=f"{m.name} 可以哪天出发？请提供具体日期或完整起止日期。"))
    if m.ret_date_start is None or m.ret_date_end is None:
        issues.append(ValidationIssue(tag, "返程日期窗不完整", question=f"{m.name} 可以哪天返程？请提供具体日期或完整起止日期。"))

    # 日期窗自洽性
    if (m.dep_date_start and m.dep_date_end
            and m.dep_date_start > m.dep_date_end):
        issues.append(ValidationIssue(
            tag, f"{m.name} 出发日期窗颠倒："
                 f"{m.dep_date_start} > {m.dep_date_end}"))
    if (m.ret_date_start and m.ret_date_end
            and m.ret_date_start > m.ret_date_end):
        issues.append(ValidationIssue(
            tag, f"{m.name} 返程日期窗颠倒："
                 f"{m.ret_date_start} > {m.ret_date_end}"))

    # 去程不得早于今天（过去日期实测会被接口拒绝："出发日期非法"）
    if m.dep_date_start and m.dep_date_start < today and (m.dep_date_end is None or m.dep_date_end < today):
        issues.append(ValidationIssue(
            tag, f"{m.name} 的出发日期 {m.dep_date_start} 早于今天 {today}",
            question=f"{m.name} 最早哪天能出发？（今天是 {today}）"))
    if m.dep_date_end and m.dep_date_end < today:
        issues.append(ValidationIssue(
            tag, f"{m.name} 的出发窗结束日 {m.dep_date_end} 早于今天 {today}",
            blocking=True))

    # 返程窗不得早于出发窗的开始日。
    # 注意下界是 dep_date_start 而非 dep_date_end：出发窗是"可以在哪天走"，
    # 用 dep_date_end 比较会错误地强制成员必须在最后一天出发。
    # 具体能否凑成 dep+nights 落在返程窗内，由 select_date_pairs 精确求解。
    if m.ret_date_end and m.dep_date_start and m.ret_date_end < m.dep_date_start:
        issues.append(ValidationIssue(
            tag,
            f"{m.name} 的返程结束日 {m.ret_date_end} 早于出发开始日 "
            f"{m.dep_date_start}，无法形成往返",
            question=f"{m.name} 打算哪天返程？"))

    # back_home_by 必须晚于返程窗开始
    if m.back_home_by and m.ret_date_start:
        if m.back_home_by.date() < m.ret_date_start:
            issues.append(ValidationIssue(
                tag,
                f"{m.name} 的到家截止时间 {m.back_home_by} 早于返程开始日 "
                f"{m.ret_date_start}"))
    return issues


def validate_request(req: TripRequest, today: Optional[date] = None) -> List[ValidationIssue]:
    """校验整个请求。返回问题列表，空列表表示可以开始查询。"""
    today = today or req.today or date.today()
    issues: List[ValidationIssue] = []

    if req.n_members < MIN_MEMBERS:
        issues.append(ValidationIssue(
            "members", f"至少需要 {MIN_MEMBERS} 位成员，当前 {req.n_members}",
            question="还有谁一起出行？请提供每位成员的出发城市。"))
    if req.n_members > MAX_MEMBERS:
        issues.append(ValidationIssue(
            "members", f"首版最多支持 {MAX_MEMBERS} 位成员，当前 {req.n_members}"))

    names = [m.name for m in req.members]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        issues.append(ValidationIssue("members", f"成员名称重复：{sorted(dupes)}"))

    if any(not c.strip() for c in req.candidates) or len(set(req.candidates)) != len(req.candidates):
        issues.append(ValidationIssue("candidates", "候选城市不得为空或重复"))
    for field_name in ("arrival_buffer_minutes", "departure_buffer_minutes", "home_buffer_minutes"):
        val = getattr(req, field_name)
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            issues.append(ValidationIssue(field_name, "到站/离站缓冲必须为非负整数分钟"))
    if req.hotel_quote is not None:
        issues.extend(_validate_hotel_quote(req))

    if req.candidates and all(m.origin_city == req.candidates[0] for m in req.members) and len(req.candidates) == 1:
        issues.append(ValidationIssue("candidates", "纯本地会合缺少实际可见面时间，不能用交通时刻代替", question="所有人都在本地，请改选旅行目的地；纯本地见面暂不属于该工具的规划范围。"))
    if req.candidates and len(req.candidates) > MAX_CANDIDATES:
        issues.append(ValidationIssue(
            "candidates",
            f"首版最多比较 {MAX_CANDIDATES} 个候选城市，当前 {len(req.candidates)}",
            question=f"候选城市超过 {MAX_CANDIDATES} 个，请挑选最想去的几个。"))

    for m in req.members:
        issues.extend(_validate_member(m, today))

    # 候选城市与出发城市重合时无意义（本地不需要会合交通）
    origins = {m.origin_city for m in req.members}
    overlap = [c for c in req.candidates if c in origins]
    if overlap:
        issues.append(ValidationIssue(
            "candidates",
            f"候选城市 {overlap} 与成员出发城市重合，该城市下成员无需长途交通",
            blocking=False))

    if req.team_budget is not None and req.team_budget <= 0:
        issues.append(ValidationIssue("team_budget", "团队总预算必须为正数"))

    # 住宿：房间数与晚数必须明确，否则无法算住宿总额
    if req.rooming is None:
        issues.append(ValidationIssue(
            "rooming",
            "未提供住宿安排，无法计算住宿费用与人均分摊",
            question=f"{req.n_members} 位成员需要几间房？（例如 2 间双人房）"))
    else:
        rp = req.rooming
        if rp.rooms <= 0:
            issues.append(ValidationIssue("rooming", "房间数必须明确且为正", question="需要几间房？"))
        if rp.nights <= 0:
            issues.append(ValidationIssue("rooming", "入住晚数必须为正"))
        if rp.nights > MAX_NIGHTS:
            issues.append(ValidationIssue(
                "rooming",
                f"首版只做 {MAX_NIGHTS} 晚以内的短途，当前 {rp.nights} 晚",
                blocking=True))
        total_beds = rp.rooms * 2  # 保守按每房 2 人计
        if req.n_members > total_beds:
            issues.append(ValidationIssue(
                "rooming",
                f"{req.n_members} 位成员住 {rp.rooms} 间房（按每房 2 人计 {total_beds} 床位）"
                f"可能不够，请确认房型",
                question=f"{rp.rooms} 间房能住下 {req.n_members} 人吗？需要什么房型？"))
        if rp.assignments:
            extra = set(rp.assignments) - set(names)
            if extra:
                issues.append(ValidationIssue("rooming", f"房间分配出现未知成员：{sorted(extra)}"))
            for room_name, residents in rp.occupancy().items():
                if len(residents) > 2:
                    issues.append(ValidationIssue("rooming", f"{room_name} 分配 {len(residents)} 人，超过当前每房最多2人的范围"))
            for room in rp.assignments.values():
                if room not in rp.room_labels:
                    issues.append(ValidationIssue(
                        "rooming", f"房间标签 {room!r} 不在 {rp.room_labels} 中"))
            missing = [m.name for m in req.members if m.name not in rp.assignments]
            if missing:
                issues.append(ValidationIssue(
                    "rooming", f"以下成员未分配房间：{missing}",
                    question=f"{missing} 分别和谁住一间？"))

    # 晚数一致性
    if req.nights is not None and req.rooming is not None:
        if req.nights != req.rooming.nights:
            issues.append(ValidationIssue(
                "nights",
                f"nights={req.nights} 与 rooming.nights={req.rooming.nights} 不一致"))

    if req.min_common_hours <= 0:
        issues.append(ValidationIssue("min_common_hours", "最少共同停留时间必须为正"))

    return issues


def _validate_hotel_quote(req: TripRequest) -> List[ValidationIssue]:
    q = req.hotel_quote
    issues = []
    if not isinstance(q, dict):
        return [ValidationIssue("hotel_quote", "酒店确认报价必须是对象")]
    valid = q.get("confirmed") is True
    if "late_checkin_confirmed" in q:
        valid = valid and isinstance(q["late_checkin_confirmed"], bool)
    valid = valid and all(isinstance(q.get(k), str) and q[k].strip() for k in ("hotel_name", "city", "source"))
    try:
        price = q.get("room_price_per_night")
        valid = valid and not isinstance(price, bool) and math.isfinite(float(price)) and float(price) > 0
        guests = q.get("max_guests_per_room")
        valid = valid and not isinstance(guests, bool) and isinstance(guests, int) and guests > 0
        check_in = date.fromisoformat(q["check_in_date"])
        check_out = date.fromisoformat(q["check_out_date"])
        valid = valid and check_out > check_in and q.get("city") in req.candidates
        valid = valid and (req.nights is None or (check_out - check_in).days == req.nights)
        if req.rooming and isinstance(guests, int):
            maximum = max((len(v) for v in req.rooming.occupancy().values()), default=math.ceil(req.n_members / max(req.rooming.rooms, 1)))
            valid = valid and maximum <= guests
    except (ValueError, TypeError, KeyError):
        valid = False
    if not valid:
        issues.append(ValidationIssue("hotel_quote", "整房确认报价缺失、日期/人数不匹配或未明确确认", question="请确认酒店名称、城市、入住日期、每房每晚价格、可住人数和报价来源；否则删除hotel_quote保留住宿待核实。"))
    return issues


def blocking_issues(issues: List[ValidationIssue]) -> List[ValidationIssue]:
    return [i for i in issues if i.blocking]


def questions_for_user(issues: List[ValidationIssue]) -> List[str]:
    """提取需要补问用户的问题（去重、保序）。只问影响可行性的必要信息。"""
    seen = set()
    out: List[str] = []
    for i in issues:
        if i.blocking and i.question and i.question not in seen:
            seen.add(i.question)
            out.append(i.question)
    return out
