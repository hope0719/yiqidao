"""会合方案规划：查询编排 + 硬性条件校验。

核心流程（对应需求"四、核心任务流程"）：
  1. 由成员时间窗求出可行的 (出发日, 返程日) 日期对；无交集则直接判无解，
     不发起任何查询。
  2. 为每位成员、每个候选城市查询**去程与返程**（绝不只查去程）。
  3. 逐条校验硬性条件，剔除不可行选项。
  4. 计算共同停留时间、最辛苦成员负担、已知/待核实费用。

实测约束（务必遵守）：
  * 带 --back-date 的往返一次查询会失败，故去程/返程拆成两次单程查询。
  * 火车 quantity（余票）恒为 null，机票无余票字段 -> 不得声称库存已确认。
  * 酒店搜索price只是展示价；仅用户确认整房报价后按晚数和房间数计算。
  * 价格缺失为 None，不参与加总，只计入"待核实"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from flyai_client import FlyAIClient, QueryResult
from connection import connection_notes, member_connection_risk
from normalize import (
    HotelOption,
    PoiOption,
    TransportOption,
    normalize_hotels,
    normalize_pois,
    normalize_transport,
)
from tripmodel import (
    MAX_NIGHTS,
    MODE_FLIGHT,
    MODE_TRAIN,
    Member,
    RoomingPlan,
    TripRequest,
)

# 每个 (成员, 方向, 交通方式) 发起的排序查询。
# 3=价格升序，6=出发早->晚，7=出发晚->早。
#
# 默认只发一个 sort=3（价格升序）。实测发现：请求越密集越容易触发
# 飞猪风控（HTTP 451），因此把查询量压到最低——3 城市 × 3 成员 ×
# 2 方式 × 2 方向 × 1 排序 = 36 次。
# 代价是"共同停留时间较长"策略只能从价格序结果里挑时刻最优项，
# 不一定全局最优。需要更精确时用 time_optimal=True 追加时刻排序查询
# （查询量翻倍），这一点在输出中如实说明，不隐瞒。
OUTBOUND_SORTS = (3,)
RETURN_SORTS = (3,)
OUTBOUND_SORTS_TIME_OPTIMAL = (3, 6)
RETURN_SORTS_TIME_OPTIMAL = (3, 7)

# 每个查询保留的候选条数上限，控制组合规模
MAX_OPTIONS_PER_QUERY = 10

# 无法从接口获知、必须标注为"未核实"的费用项
UNVERIFIED_COST_ITEMS = (
    "市内交通与机场/车站接驳",
    "餐饮",
    "景点门票（未选定具体项目）",
    "行李、保险等其他杂费",
)

MODE_TO_ENDPOINT = {MODE_FLIGHT: "search-flight", MODE_TRAIN: "search-train"}
MODE_LABEL = {MODE_FLIGHT: "飞机", MODE_TRAIN: "火车"}


@dataclass
class DatePair:
    """一组可行的出发/返程日期。"""

    dep_date: date
    ret_date: date
    nights: int

    @property
    def span_days(self) -> int:
        return (self.ret_date - self.dep_date).days


@dataclass
class MemberLeg:
    """某位成员在某个方向上的全部候选与筛选结果。"""

    member: str
    direction: str  # outbound | return
    options: List[TransportOption] = field(default_factory=list)
    feasible: List[TransportOption] = field(default_factory=list)
    provenance: List[Dict[str, Any]] = field(default_factory=list)
    query_failures: List[str] = field(default_factory=list)
    reject_reasons: List[str] = field(default_factory=list)


@dataclass
class MemberPlan:
    """某位成员在某候选城市下的完整往返安排。"""

    name: str
    origin: str
    outbound: Optional[TransportOption] = None
    ret: Optional[TransportOption] = None
    transport_known: Optional[float] = None
    transport_missing: bool = False
    total_minutes: Optional[int] = None
    worst_oneway_minutes: Optional[int] = None
    lodging_share: Optional[float] = None
    lodging_missing: bool = False
    known_cost: Optional[float] = None
    cost_incomplete: bool = False
    unverified: List[str] = field(default_factory=list)
    budget: Optional[float] = None
    budget_state: str = "unknown"  # ok | over | unknown
    budget_delta: Optional[float] = None
    is_local: bool = False
    violations: List[str] = field(default_factory=list)
    query_notes: List[str] = field(default_factory=list)  # 非致命的查询失败，仅供披露
    arrive_at: Optional[datetime] = None
    depart_at: Optional[datetime] = None

    @property
    def feasible(self) -> bool:
        return (
            (self.is_local or (self.outbound is not None and self.ret is not None))
            and not self.violations
        )


@dataclass
class CityPlan:
    """一个候选城市的完整方案。"""

    city: str
    date_pair: DatePair
    members: List[MemberPlan] = field(default_factory=list)
    common_start: Optional[datetime] = None
    common_end: Optional[datetime] = None
    common_hours: Optional[float] = None
    worst_burden_minutes: Optional[int] = None
    worst_burden_member: Optional[str] = None
    team_known_cost: Optional[float] = None
    team_cost_incomplete: bool = False
    team_unverified: List[str] = field(default_factory=list)
    feasible: bool = False
    blockers: List[str] = field(default_factory=list)
    hotels: List[HotelOption] = field(default_factory=list)
    chosen_hotel: Optional[HotelOption] = None
    hotel_query: Any = None
    hotel_choice_rationale: Optional[str] = None
    pois: List[PoiOption] = field(default_factory=list)
    poi_query: Optional[Dict[str, Any]] = None
    strategy: str = "cheapest"
    provenance: List[Dict[str, Any]] = field(default_factory=list)
    query_notes: List[str] = field(default_factory=list)  # 部分查询失败但不致命
    rooming: Optional[RoomingPlan] = None
    split_method: str = "equal_per_member"
    confirmation_required: List[str] = field(default_factory=list)


def select_date_pairs(req: TripRequest, today: date) -> Tuple[List[DatePair], List[str]]:
    """求所有成员时间窗的交集，枚举可行的 (出发日, 返程日) 对。

    返回 (可行日期对列表, 阻塞原因列表)。列表为空说明时间上就无解，
    此时不应发起任何查询。
    """
    blockers: List[str] = []
    nights = req.nights or (req.rooming.nights if req.rooming else None)
    if not nights:
        blockers.append("未指定行程晚数（2-3 天短途通常为 1-2 晚）")
        return [], blockers
    if nights > MAX_NIGHTS:
        blockers.append(f"首版仅支持 {MAX_NIGHTS} 晚以内，当前 {nights} 晚")

    dep_lo: Optional[date] = None
    dep_hi: Optional[date] = None
    ret_lo: Optional[date] = None
    ret_hi: Optional[date] = None
    for m in req.members:
        if m.dep_date_start:
            dep_lo = m.dep_date_start if dep_lo is None else max(dep_lo, m.dep_date_start)
        if m.dep_date_end:
            dep_hi = m.dep_date_end if dep_hi is None else min(dep_hi, m.dep_date_end)
        if m.ret_date_start:
            ret_lo = m.ret_date_start if ret_lo is None else max(ret_lo, m.ret_date_start)
        if m.ret_date_end:
            ret_hi = m.ret_date_end if ret_hi is None else min(ret_hi, m.ret_date_end)

    if dep_lo is None:
        dep_lo = today
    if dep_lo < today:
        dep_lo = today
    if dep_hi is None:
        dep_hi = dep_lo + timedelta(days=MAX_NIGHTS + 1)

    if dep_hi < dep_lo:
        blockers.append(
            f"成员出发时间窗无交集：最早可行 {dep_lo}，最晚 {dep_hi}。"
            f"请放宽某位成员的出发日期。"
        )
        return [], blockers

    pairs: List[DatePair] = []
    d = dep_lo
    while d <= dep_hi:
        r = d + timedelta(days=nights)
        ok = True
        if ret_lo and r < ret_lo:
            ok = False
        if ret_hi and r > ret_hi:
            ok = False
        for m in req.members:
            if m.back_home_by and datetime.combine(r, datetime.max.time()) > m.back_home_by:
                # 返程当天到家截止时间过紧，交由后续逐班次校验，这里不直接排除
                pass
        if ok:
            pairs.append(DatePair(dep_date=d, ret_date=r, nights=nights))
        d += timedelta(days=1)

    if not pairs:
        parts = []
        if ret_lo or ret_hi:
            parts.append(f"返程窗 {ret_lo or '-'} ~ {ret_hi or '-'}")
        parts.append(f"出发窗 {dep_lo} ~ {dep_hi}")
        parts.append(f"行程 {nights} 晚")
        blockers.append(
            "在成员时间窗内找不到满足 " + "、".join(parts) + " 的日期组合。"
            "可放宽返程日期或减少晚数。"
        )
    return pairs, blockers


# --------------------------------------------------------------------------
# 交通查询
# --------------------------------------------------------------------------

def _query_leg(
    client: FlyAIClient,
    member: Member,
    mode: str,
    direction: str,
    origin: str,
    destination: str,
    travel_date: date,
    sorts: Tuple[int, ...],
    cached: bool = True,
) -> MemberLeg:
    """查询一位成员某方向的全部候选（多排序、多方式）。"""
    leg = MemberLeg(member=member.name, direction=direction)
    endpoint = MODE_TO_ENDPOINT[mode]
    o, d = (origin, destination) if direction == "outbound" else (destination, origin)

    # 注意：不要把成员时长上限下推为 --total-duration-hour。
    # 实测（2026-10-10）该参数虽在 flyai search-flight --help 中列出，
    # 但机票端点后端会拒绝并返回 HTTP 400
    # "Argument not allowed: [totalDurationHour]"；火车端点则接受。
    # CLI 帮助与后端实际接受的参数并不一致，故一律改为本地过滤。
    for sort_type in sorts:
        params: Dict[str, Any] = {
            "origin": o,
            "destination": d,
            "dep_date": travel_date.isoformat(),
            "sort_type": sort_type,
        }
        fn = client.query_cached if cached else client.query
        res: QueryResult = fn(endpoint, **params)
        leg.provenance.append(res.provenance)
        if not res.ok:
            leg.query_failures.append(
                f"{MODE_LABEL[mode]} {o}->{d} {travel_date} "
                f"(sort={sort_type}): {res.failure}"
            )
            continue
        opts = normalize_transport(res.items, mode, direction)
        valid = []
        for opt in opts:
            actual_o = opt.segments[0].dep_city
            actual_d = opt.segments[-1].arr_city
            if actual_o and actual_o.removesuffix("市") != o.removesuffix("市"):
                continue
            if actual_d and actual_d.removesuffix("市") != d.removesuffix("市"):
                continue
            valid.append(opt)
        if res.items and not valid:
            note = "接口返回了条目，但时刻/路线无法用于本次请求，不能据此确定没有班次"
            leg.query_failures.append(note)
            leg.provenance[-1].update(ok=False, failure=note, failure_kind="parse")
        leg.options.extend(valid[:MAX_OPTIONS_PER_QUERY])

    # 同价同班次去重
    seen = set()
    uniq: List[TransportOption] = []
    for o_ in leg.options:
        key = (o_.mode, o_.transport_summary,
               str(o_.dep_time), str(o_.arr_time), o_.price)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(o_)
    leg.options = uniq
    return leg


def _filter_leg(leg: MemberLeg, member: Member, pair: DatePair, home_buffer_minutes: int = 0) -> MemberLeg:
    """按成员硬性条件筛选候选。被剔除的原因逐条记录，便于向用户解释无解。"""
    reasons: List[str] = []
    for opt in leg.options:
        bad = None

        # 日期必须落在请求的日期对上（接口偶发返回邻近日期）
        if leg.direction == "outbound":
            if opt.dep_time and opt.dep_time.date() != pair.dep_date:
                bad = f"出发日 {opt.dep_time.date()} 不在 {pair.dep_date}"
        else:
            if opt.dep_time and opt.dep_time.date() != pair.ret_date:
                bad = f"返程日 {opt.dep_time.date()} 不在 {pair.ret_date}"

        route_origin = getattr(opt.segments[0], "dep_city", None) if opt.segments else None
        route_destination = getattr(opt.segments[-1], "arr_city", None) if opt.segments else None
        expected_home = route_origin if leg.direction == "outbound" else route_destination
        if not bad and expected_home and expected_home.removesuffix("市") != member.origin_city.removesuffix("市"):
            bad = "返回班次的出发/到家城市与成员条件不符"

        # 交通方式必须在成员允许范围内
        if not bad and opt.mode not in member.modes:
            bad = f"交通方式 {MODE_LABEL.get(opt.mode, opt.mode)} 不在允许范围"

        # 单程时长上限
        if (not bad and member.max_oneway_minutes
                and opt.total_duration_min
                and opt.total_duration_min > member.max_oneway_minutes):
            bad = (f"单程 {opt.total_duration_min} 分钟 > 上限 "
                   f"{member.max_oneway_minutes} 分钟")

        if not bad and member.max_oneway_minutes and opt.total_duration_min is None:
            bad = "时长缺失，无法核验单程时长上限"

        # 到家截止时间（作用于返程抵达出发城市的时刻）
        if not bad and leg.direction == "return":
            if member.back_home_by and opt.arr_time and opt.arr_time + timedelta(minutes=home_buffer_minutes) > member.back_home_by:
                bad = (f"到站后预留{home_buffer_minutes}分钟，到家估计 {(opt.arr_time + timedelta(minutes=home_buffer_minutes)):%m-%d %H:%M} 晚于截止 "
                       f"{member.back_home_by:%m-%d %H:%M}")
            elif member.ret_date_end and opt.arr_time and opt.arr_time.date() > member.ret_date_end:
                bad = f"到家日 {opt.arr_time.date()} 晚于返程窗结束 {member.ret_date_end}"

        if bad:
            reasons.append(f"{opt.transport_summary}: {bad}")
        else:
            leg.feasible.append(opt)
    leg.reject_reasons = reasons
    return leg


# --------------------------------------------------------------------------
# 策略选择
# --------------------------------------------------------------------------

def _pick_cheapest(options: List[TransportOption]) -> Optional[TransportOption]:
    """价格最低优先；价格缺失的排到最后（绝不当作 0 元）。"""
    if not options:
        return None
    return min(
        options,
        key=lambda o: (o.price is None, o.price if o.price is not None else float("inf")),
    )


def _pick_earliest_arrival(options: List[TransportOption]) -> Optional[TransportOption]:
    """最早到达，用于最大化共同停留时间。"""
    timed = [o for o in options if o.arr_time]
    if not timed:
        return None
    return min(timed, key=lambda o: o.arr_time)


def _pick_latest_departure(options: List[TransportOption]) -> Optional[TransportOption]:
    """最晚出发，用于最大化共同停留时间。"""
    timed = [o for o in options if o.dep_time]
    if not timed:
        return None
    return max(timed, key=lambda o: o.dep_time)


def _pick_shortest(options: List[TransportOption]) -> Optional[TransportOption]:
    """耗时最短，用于降低最辛苦成员的交通负担。"""
    if not options:
        return None
    return min(
        options,
        key=lambda o: (
            o.total_duration_min is None,
            o.total_duration_min if o.total_duration_min is not None else 10 ** 9,
            o.price if o.price is not None else float("inf"),
        ),
    )


STRATEGY_PICKERS = {
    "cheapest": (_pick_cheapest, _pick_cheapest),
    "max_common": (_pick_earliest_arrival, _pick_latest_departure),
    "min_burden": (_pick_shortest, _pick_shortest),
}
STRATEGY_LABEL = {
    "cheapest": "已知花费较低",
    "max_common": "共同停留时间较长",
    "min_burden": "交通负担更均衡",
}


def collect_legs(
    client: FlyAIClient,
    req: TripRequest,
    city: str,
    pair: DatePair,
    cached: bool = True,
    time_optimal: bool = False,
) -> Dict[str, Dict[str, MemberLeg]]:
    """为所有成员查询去程+返程，返回 {member_name: {"outbound": leg, "return": leg}}。

    time_optimal=True 时追加时刻排序查询（查询量翻倍），用于更精确地
    最大化共同停留时间。默认 False 以控制请求数、规避风控。
    """
    ob_sorts = OUTBOUND_SORTS_TIME_OPTIMAL if time_optimal else OUTBOUND_SORTS
    rt_sorts = RETURN_SORTS_TIME_OPTIMAL if time_optimal else RETURN_SORTS
    out: Dict[str, Dict[str, MemberLeg]] = {}
    for m in req.members:
        if m.origin_city == city:
            # 本地成员无需长途交通
            empty_ob = MemberLeg(member=m.name, direction="outbound")
            empty_rt = MemberLeg(member=m.name, direction="return")
            out[m.name] = {"outbound": empty_ob, "return": empty_rt}
            continue
        ob = MemberLeg(member=m.name, direction="outbound")
        rt = MemberLeg(member=m.name, direction="return")
        for mode in m.modes:
            sub_ob = _query_leg(client, m, mode, "outbound", m.origin_city, city,
                                pair.dep_date, ob_sorts, cached)
            sub_rt = _query_leg(client, m, mode, "return", m.origin_city, city,
                                pair.ret_date, rt_sorts, cached)
            ob.options.extend(sub_ob.options)
            ob.provenance.extend(sub_ob.provenance)
            ob.query_failures.extend(sub_ob.query_failures)
            rt.options.extend(sub_rt.options)
            rt.provenance.extend(sub_rt.provenance)
            rt.query_failures.extend(sub_rt.query_failures)
        out[m.name] = {"outbound": _filter_leg(ob, m, pair, req.home_buffer_minutes),
                       "return": _filter_leg(rt, m, pair, req.home_buffer_minutes)}
    return out


# --------------------------------------------------------------------------
# 住宿与费用计算
# --------------------------------------------------------------------------

def compute_lodging_total(hotel: Optional[HotelOption], rooming: RoomingPlan) -> Tuple[Optional[float], bool]:
    """住宿总额 = 每晚单价 * 晚数 * 房间数。

    仅明确确认的整房每晚报价参与计算；接口搜索展示价不能当房价。
    """
    if hotel is None or not hotel.quote_confirmed or hotel.price_per_night is None:
        return None, True
    return hotel.price_per_night * rooming.nights * rooming.rooms, False


def compute_lodging_share(
    hotel: Optional[HotelOption],
    rooming: RoomingPlan,
    member: Member,
    n_members: int,
    split_method: str = "equal_per_member",
) -> Tuple[Optional[float], bool, str]:
    """计算某位成员的住宿分摊。

    split_method:
      * equal_per_member —— 住宿总额按人数均摊（默认，最常用）
      * per_room         —— 先按房间均摊，再按房间内人数均摊

    返回 (分摊额, 是否缺失, 说明)。房间分配不明时不做臆测，退回均摊并注明。
    """
    total, missing = compute_lodging_total(hotel, rooming)
    if missing or total is None:
        return None, True, "整房每晚报价未确认，展示价不参与分摊"

    if split_method == "per_room" and rooming.assignments:
        occ = rooming.occupancy()
        room_of = rooming.assignments.get(member.name)
        if room_of and occ.get(room_of):
            per_room = total / rooming.rooms
            share = per_room / len(occ[room_of])
            return share, False, (
                f"按房间分摊：{room_of} 共 {len(occ[room_of])} 人，"
                f"房费 {per_room:.0f} 元均摊"
            )
        return total / n_members, False, "房间分配不完整，退回按人数均摊"

    return total / n_members, False, f"住宿总额 {total:.0f} 元按 {n_members} 人均摊"


def build_member_plan(
    member: Member,
    legs: Dict[str, MemberLeg],
    strategy: str,
    hotel: Optional[HotelOption],
    rooming: Optional[RoomingPlan],
    n_members: int,
    split_method: str,
) -> MemberPlan:
    """组装一位成员的往返安排，并做逐人预算校验。"""
    plan = MemberPlan(
        name=member.name, origin=member.origin_city, budget=member.budget
    )
    ob_pick, rt_pick = STRATEGY_PICKERS[strategy]

    ob_leg = legs.get("outbound")
    rt_leg = legs.get("return")

    # 本地成员：无长途交通，费用为 0（这是确定的，不是缺失）
    local_member = ob_leg is not None and not ob_leg.options and not ob_leg.provenance
    if local_member:
        plan.is_local = True
        plan.transport_known = 0.0
        plan.transport_missing = False
        plan.total_minutes = 0
        plan.worst_oneway_minutes = 0
        plan.arrive_at = None
        plan.depart_at = None
        if ob_leg.query_failures or (rt_leg and rt_leg.query_failures):
            plan.violations.extend(ob_leg.query_failures or [])

    if not local_member:
        if ob_leg:
            plan.outbound = ob_pick(ob_leg.feasible)
        if rt_leg:
            plan.ret = rt_pick(rt_leg.feasible)

        # 记录查询失败，但**不直接判为违规**：成员可能同时接受飞机与火车，
        # 其中一种方式失败不应否决整个方案（实测：杭州->上海 机票 status=1，
        # 火车正常返回 10 条）。只有当该方向最终没有任何可行班次时，
        # 才把失败信息作为原因之一呈现。
        if ob_leg:
            plan.query_notes.extend(ob_leg.query_failures)
        if rt_leg:
            plan.query_notes.extend(rt_leg.query_failures)

        if plan.outbound is None:
            detail = ""
            if ob_leg:
                detail = (f"（{len(ob_leg.options)} 个候选全被硬性条件剔除）"
                          if ob_leg.options else "（未取到任何候选）")
            msg = f"{member.name} 无满足条件的去程班次{detail}"
            if ob_leg and ob_leg.query_failures:
                msg += "；相关查询失败：" + "；".join(ob_leg.query_failures[:2])
            plan.violations.append(msg)
        if plan.ret is None:
            detail = ""
            if rt_leg:
                detail = (f"（{len(rt_leg.options)} 个候选全被硬性条件剔除）"
                          if rt_leg.options else "（未取到任何候选）")
            msg = f"{member.name} 无满足条件的返程班次{detail}"
            if rt_leg and rt_leg.query_failures:
                msg += "；相关查询失败：" + "；".join(rt_leg.query_failures[:2])
            plan.violations.append(msg)

        # 交通费用加总：缺失价格记为 None，绝不当 0
        known = 0.0
        missing_any = False
        for opt in (plan.outbound, plan.ret):
            if opt is None:
                continue
            if opt.price is None:
                missing_any = True
            else:
                known += opt.price
        plan.transport_known = None if (missing_any and known == 0) else known
        plan.transport_missing = missing_any

        # 时长
        durs = [o.total_duration_min for o in (plan.outbound, plan.ret)
                if o is not None and o.total_duration_min is not None]
        plan.total_minutes = sum(durs) if durs else None
        plan.worst_oneway_minutes = max(durs) if durs else None

        # 往返总时长上限校验（与单程上限分别处理）
        if (member.max_roundtrip_minutes and plan.total_minutes
                and plan.total_minutes > member.max_roundtrip_minutes):
            plan.violations.append(
                f"{member.name} 往返合计 {plan.total_minutes} 分钟 > 上限 "
                f"{member.max_roundtrip_minutes} 分钟"
            )

        plan.arrive_at = plan.outbound.arr_time if plan.outbound else None
        plan.depart_at = plan.ret.dep_time if plan.ret else None


    # 住宿分摊
    if rooming:
        share, miss, note = compute_lodging_share(
            hotel, rooming, member, n_members, split_method
        )
        plan.lodging_share = share
        plan.lodging_missing = miss
        if miss:
            plan.unverified.append(f"住宿费用（{note}）")

    # 已知费用小计 = 交通已知部分 + 住宿分摊。
    # 任一项缺失时 known_cost 仍是"已知部分之和"，但 cost_incomplete=True，
    # 此时不得宣称满足预算，必须同时展示待核实费用。
    parts: List[float] = []
    incomplete = plan.transport_missing
    if plan.transport_known is not None:
        parts.append(plan.transport_known)
    elif plan.outbound is not None or plan.ret is not None:
        incomplete = True
    if plan.lodging_share is not None:
        parts.append(plan.lodging_share)
    elif rooming is not None:
        incomplete = True
    plan.known_cost = round(sum(parts), 2) if parts else None
    plan.cost_incomplete = incomplete

    # 未核实费用项（接口无法提供）
    for item in UNVERIFIED_COST_ITEMS:
        if item not in plan.unverified:
            plan.unverified.append(item)

    # 逐人预算校验：已知费用超过预算即违规；费用不完整则不能宣称满足
    if member.budget is not None:
        if plan.known_cost is not None and plan.known_cost > member.budget:
            plan.budget_state = "over"
            plan.budget_delta = round(member.budget - plan.known_cost, 2)
            plan.violations.append(
                f"{member.name} 已知费用 {plan.known_cost:.0f} 元已超个人预算 "
                f"{member.budget:.0f} 元（超 {abs(plan.budget_delta):.0f} 元）"
            )
        elif incomplete:
            plan.budget_state = "unknown"
        else:
            plan.budget_state = "ok"
            plan.budget_delta = round(member.budget - (plan.known_cost or 0.0), 2)
    return plan


def compute_common_stay(plans: List[MemberPlan], arrival_buffer_minutes: int = 0, departure_buffer_minutes: int = 0) -> Tuple[Optional[datetime], Optional[datetime], Optional[float]]:
    """共同停留时间 = 最晚到达成员 -> 最早离开成员。

    注意：这是"人都到齐"的区间，不等于实际游玩时间。市内交通、接驳、
    入住与休息需另行扣除，接口无此数据，一律标注未核实。
    """
    arrivals = [p.arrive_at for p in plans if p.arrive_at is not None]
    departures = [p.depart_at for p in plans if p.depart_at is not None]
    # 本地成员无交通时刻，不参与区间收窄
    if not arrivals or not departures:
        return None, None, None
    start = max(arrivals) + timedelta(minutes=arrival_buffer_minutes)
    end = min(departures) - timedelta(minutes=departure_buffer_minutes)
    if end <= start:
        return start, end, 0.0
    hours = (end - start).total_seconds() / 3600.0
    return start, end, round(hours, 2)


# --------------------------------------------------------------------------
# 城市方案装配
# --------------------------------------------------------------------------

def query_hotels(
    client: FlyAIClient,
    city: str,
    pair: DatePair,
    max_price: Optional[float] = None,
    cached: bool = True,
    poi_name: Optional[str] = None,
) -> Tuple[List[HotelOption], List[Dict[str, Any]]]:
    """查询候选城市酒店。check-in/out 用日期对的出发日与返程日。

    实测 sort=price_asc 只返回全城最便宜的约 10 家，其中不乏与行程目的地
    相距很远的青旅（如为西湖之行返回下沙大学城的 ¥25 旅馆）。因此在预算
    允许时追加一次 rate_desc 查询扩大候选池，兼顾价格与口碑。
    这会多一次请求，故仅在未指定 max_price 上限过窄时执行。
    """
    fn = client.query_cached if cached else client.query
    provs: List[Dict[str, Any]] = []
    hotels: List[HotelOption] = []

    base: Dict[str, Any] = {
        "dest_name": city,
        "check_in_date": pair.dep_date.isoformat(),
        "check_out_date": pair.ret_date.isoformat(),
    }
    if poi_name:
        base["poi_name"] = poi_name

    for sort_key in ("price_asc", "rate_desc"):
        params = dict(base)
        params["sort"] = sort_key
        if max_price:
            params["max_price"] = int(max_price)
        res = fn("search-hotel", **params)
        provs.append(res.provenance)
        if res.ok:
            hotels.extend(normalize_hotels(res.items))
        # 价格序已给出可用候选时，口碑序属于锦上添花；失败不影响主流程
    # 去重（同一酒店可能同时出现在两次查询里）
    seen = set()
    uniq: List[HotelOption] = []
    for h in hotels:
        key = h.hotel_id or h.name
        if key in seen:
            continue
        seen.add(key)
        uniq.append(h)
    return uniq, provs


def query_pois(
    client: FlyAIClient, city: str, cached: bool = True
) -> Tuple[List[PoiOption], Dict[str, Any]]:
    fn = client.query_cached if cached else client.query
    res = fn("search-poi", city_name=city)
    pois = normalize_pois(res.items) if res.ok else []
    return pois, res.provenance


def build_city_plan(
    client: FlyAIClient,
    req: TripRequest,
    city: str,
    pair: DatePair,
    legs_by_member: Dict[str, Dict[str, MemberLeg]],
    strategy: str,
    hotel: Optional[HotelOption],
    cached: bool = True,
    selected_members: Optional[List[MemberPlan]] = None,
) -> CityPlan:
    """装配一个候选城市的完整方案。"""
    plan = CityPlan(city=city, date_pair=pair, strategy=strategy, rooming=req.rooming)
    plan.split_method = "equal_per_member"

    for m in req.members:
        legs = legs_by_member.get(m.name, {})
        mp = build_member_plan(
            m, legs, strategy, hotel, req.rooming, req.n_members, plan.split_method
        )
        if selected_members is not None:
            mp = next(p for p in selected_members if p.name == m.name)
        plan.members.append(mp)
        plan.query_notes.extend(mp.query_notes)
        for direction in ("outbound", "return"):
            leg = legs.get(direction)
            if leg:
                plan.provenance.extend(leg.provenance)

    # 共同停留时间（代码计算，不心算）
    plan.common_start, plan.common_end, plan.common_hours = compute_common_stay(plan.members, req.arrival_buffer_minutes, req.departure_buffer_minutes)

    # 最辛苦成员的交通负担 = 往返总时长的最大值
    burdens = [(p.total_minutes, p.name) for p in plan.members if p.total_minutes is not None]
    if burdens:
        worst = max(burdens, key=lambda x: x[0])
        plan.worst_burden_minutes, plan.worst_burden_member = worst

    # 团队已知费用 = 各成员已知费用之和（缺失者按 0 计入但标记不完整）
    team_known = 0.0
    incomplete = False
    for p in plan.members:
        if p.known_cost is None:
            incomplete = True
        else:
            team_known += p.known_cost
        if p.cost_incomplete:
            incomplete = True
    plan.team_known_cost = round(team_known, 2)
    plan.team_cost_incomplete = incomplete
    plan.team_unverified = list(UNVERIFIED_COST_ITEMS)

    # 可行性判定
    blockers: List[str] = []
    for p in plan.members:
        blockers.extend(p.violations)
    if plan.common_hours is None:
        blockers.append("无法确定共同停留时间（缺少到达/离开时刻）")
    elif (plan.common_end - plan.common_start).total_seconds() < req.min_common_hours * 3600:
        blockers.append(
            f"共同停留 {plan.common_hours} 小时 < 要求的 {req.min_common_hours} 小时"
        )
    for mp in plan.members:
        for label, option in (("去程", mp.outbound), ("返程", mp.ret)):
            plan.confirmation_required.extend(f"{mp.name}{label}：{note}" for note in connection_notes(option))
    if hotel is None or not hotel.quote_confirmed:
        plan.confirmation_required.append("酒店整房报价、房型人数及可订性待确认；展示价不计入已知费用")
    if any(mp.transport_missing for mp in plan.members):
        plan.confirmation_required.append("部分交通票价缺失，完整预算待确认")
    if any(mp.arrive_at and (mp.arrive_at.date() > pair.dep_date or mp.arrive_at.hour >= 22 or mp.arrive_at.hour < 6) for mp in plan.members):
        if not (hotel and hotel.quote_confirmed and (req.hotel_quote or {}).get("late_checkin_confirmed") is True):
            plan.confirmation_required.append("部分成员深夜或跨日到达，需确认到店时间、夜间入住及保留房间")
    if req.team_budget is not None:
        if plan.team_known_cost > req.team_budget:
            blockers.append(
                f"团队已知费用 {team_known:.0f} 元 > 团队总预算 {req.team_budget:.0f} 元"
            )
    plan.blockers = blockers
    plan.feasible = not blockers
    return plan


def rank_plans(plans: List[CityPlan], req: TripRequest) -> List[CityPlan]:
    """对可行方案排序。排序键全部由代码计算。

    主序：较少成员有紧张或未知换乘风险，再比较完整价格和已知费用。
    次序：共同停留时间降序、最辛苦负担升序。
    """
    feasible = [p for p in plans if p.feasible]

    def sort_key(p: CityPlan) -> tuple:
        cost = p.team_known_cost if p.team_known_cost is not None else float("inf")
        common = -(p.common_hours if p.common_hours is not None else 0.0)
        burden = p.worst_burden_minutes if p.worst_burden_minutes is not None else float("inf")
        incomplete = 1 if p.team_cost_incomplete else 0
        transport_missing = any(member.transport_missing for member in p.members)
        return (sum(member_connection_risk(m) for m in p.members), transport_missing, incomplete, cost, common, burden)

    return sorted(feasible, key=sort_key)


def diagnose_infeasible(plans: List[CityPlan], req: TripRequest) -> Dict[str, Any]:
    """无可行方案时，归纳导致无解的条件并给出放宽建议。"""
    if not plans:
        return {
            "reason": "没有任何候选城市完成查询",
            "suggestions": ["请确认候选城市名称，或检查接口是否可用"],
            "details": [],
        }

    # 统计所有阻塞原因，按出现频次排序
    counter: Dict[str, int] = {}
    for p in plans:
        for b in p.blockers:
            counter[b] = counter.get(b, 0) + 1
    ranked = sorted(counter.items(), key=lambda kv: -kv[1])

    suggestions: List[str] = []
    joined = " ".join(counter.keys())
    if "预算" in joined or "超" in joined:
        suggestions.append("提高个人预算或团队总预算")
        suggestions.append("改用火车等更经济的交通方式")
    if "共同停留" in joined:
        suggestions.append("放宽成员的出发/返程日期窗，让大家时间更容易重叠")
        suggestions.append(f"降低最少共同停留时间要求（当前 {req.min_common_hours} 小时）")
    if "时长上限" in joined or "分钟" in joined:
        suggestions.append("放宽单程或往返交通时长上限")
    if "到家" in joined:
        suggestions.append("推迟成员的到家截止时间")
    if "酒店" in joined:
        suggestions.append("放宽酒店筛选条件（如取消最高价限制）")
    if "无满足条件" in joined:
        suggestions.append("放宽日期窗或交通方式，或增加候选城市")

    return {
        "reason": "在已查询的城市、日期和返回班次中未找到满足条件的组合",
        "blockers": [{"reason": r, "count": c} for r, c in ranked],
        "suggestions": suggestions or ["放宽任一硬性约束后重试"],
        "cities_tried": [p.city for p in plans],
    }
