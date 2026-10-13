"""编排器：把校验、查询、计算、排序串成一次完整的会合规划。

对外主入口 plan_trip(request, client, ...) -> PlanOutcome。

流程：
  1. validate_request 校验请求，有阻塞问题则直接返回"需要补问"，不查询。
  2. select_date_pairs 求时间窗交集；无交集则判无解，不查询。
  3. 逐候选城市查询去程+返程（每人每方向，飞机与火车按允许方式）。
  4. 在返回班次内枚举往返对，扫描共同时间，选择最低已知费用组合。
  5. 为可行方案查询酒店与景点，重算含住宿的费用。
  6. 排序并返回最多 max_plans 个方案。

增量重算：交通查询结果由 client.query_cached 落盘缓存。用户修改某位
成员条件后，只有该成员相关的 (endpoint, params) 组合会变化，其余命中缓存，
不会重复发起真实请求。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from flyai_client import FlyAIClient
from normalize import HotelOption, PoiOption
from solver import solve_city
from planner import (
    CityPlan,
    STRATEGY_LABEL,
    build_city_plan,
    collect_legs,
    diagnose_infeasible,
    query_hotels,
    query_pois,
    rank_plans,
    select_date_pairs,
)
from tripmodel import (
    TripRequest,
    ValidationIssue,
    blocking_issues,
    questions_for_user,
    validate_request,
)

# 策略尝试顺序：先用最省钱的；不可行再试其他
STRATEGY_ORDER = ("cheapest", "max_common", "min_burden")


@dataclass
class PlanOutcome:
    """一次规划的完整结果。"""

    status: str  # needs_input | needs_confirmation | search_incomplete | no_solution | ok | error
    plans: List[CityPlan] = field(default_factory=list)
    all_plans: List[CityPlan] = field(default_factory=list)
    questions: List[str] = field(default_factory=list)
    issues: List[Dict[str, Any]] = field(default_factory=list)
    diagnosis: Optional[Dict[str, Any]] = None
    date_pairs: List[Dict[str, Any]] = field(default_factory=list)
    provenance: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    generated_at: Optional[str] = None
    stats: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "generated_at": self.generated_at,
            "questions": self.questions,
            "issues": self.issues,
            "diagnosis": self.diagnosis,
            "date_pairs": self.date_pairs,
            "stats": self.stats,
            "n_plans": len(self.plans),
            "plans": _json_value(self.plans),
            "all_plans": _json_value(self.all_plans),
            "provenance": self.provenance,
            "error": self.error,
        }


def _json_value(value):
    if hasattr(value, "__dataclass_fields__"):
        return _json_value(asdict(value))
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_value(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(val) for val in value]
    return value


def _dedup_provenance(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for p in items:
        key = (p.get("endpoint"), str(p.get("params")), p.get("source"))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def plan_trip(
    req: TripRequest,
    client: FlyAIClient,
    today: Optional[date] = None,
    max_plans: int = 3,
    cached: bool = True,
    strategies: Optional[tuple] = None,
    time_optimal: bool = False,
    max_date_pairs: int = 3,
) -> PlanOutcome:
    """执行一次完整的会合规划。"""
    from flyai_client import _utc_now

    today = today or req.today or date.today()
    outcome = PlanOutcome(status="ok", generated_at=_utc_now())
    strategies = strategies or STRATEGY_ORDER

    # ---- 1. 请求校验 ----
    issues: List[ValidationIssue] = validate_request(req, today)
    outcome.issues = [i.to_dict() for i in issues]
    outcome.questions = questions_for_user(issues)
    if blocking_issues(issues):
        outcome.status = "needs_input"
        outcome.stats["n_blocking_issues"] = len(blocking_issues(issues))
        return outcome

    # ---- 2. 日期对 ----
    pairs, pair_blockers = select_date_pairs(req, today)
    outcome.date_pairs = [
        {
            "dep_date": p.dep_date.isoformat(),
            "ret_date": p.ret_date.isoformat(),
            "nights": p.nights,
        }
        for p in pairs
    ]
    if pair_blockers:
        outcome.stats["date_pair_blockers"] = pair_blockers
    if not pairs:
        outcome.status = "no_solution"
        outcome.diagnosis = {
            "reason": "成员时间窗无交集，无法形成可行的往返日期",
            # 日期阶段的阻塞与城市数无关，不带 count，
            # 否则渲染层会错误显示"（N 个城市）"
            "blockers": [{"reason": b} for b in pair_blockers],
            "suggestions": [
                "放宽某位成员的出发或返程日期窗",
                "缩短行程晚数",
            ],
        }
        return outcome

    if max_date_pairs <= 0:
        raise ValueError("max_date_pairs 必须为正数")
    candidates = req.candidates
    if not candidates:
        outcome.status = "needs_input"
        outcome.questions = ["请提供候选会合城市（最多5个）。"]
        return outcome
    # 有已确认住宿报价时优先搜索对应日期，其他日期仍按顺序进行。
    quote = req.hotel_quote
    ordered_pairs = list(pairs)
    if quote:
        ordered_pairs.sort(key=lambda p: not (quote["check_in_date"] == p.dep_date.isoformat() and quote["check_out_date"] == p.ret_date.isoformat()))
    selected_pairs = ordered_pairs[:max_date_pairs]
    all_plans = []
    stopped = False
    for pair in selected_pairs:
        for city in candidates:
            legs_by_member = collect_legs(client, req, city, pair, cached=cached, time_optimal=time_optimal)
            transport_available = all(
                leg.feasible or (not leg.provenance and not leg.query_failures)
                for legs in legs_by_member.values() for leg in legs.values()
            )
            hotels, hotel_provs = (query_hotels(client, city, pair, cached=cached, poi_name=_preferred_poi(req))
                                  if transport_available and not getattr(client, "stop_reason", None) else ([], []))
            if quote and quote["city"] == city and quote["check_in_date"] == pair.dep_date.isoformat() and quote["check_out_date"] == pair.ret_date.isoformat():
                hotel = HotelOption.from_confirmed_quote(quote)
                hotels.insert(0, hotel)
            else:
                hotel = _pick_hotel(hotels, req)
            plan = solve_city(client, req, city, pair, legs_by_member, hotel, cached=cached)
            plan.hotels = _room_candidates(hotels)
            plan.provenance.extend(hotel_provs)
            plan.hotel_query = hotel_provs
            plan.chosen_hotel = hotel
            plan.hotel_choice_rationale = describe_hotel_choice(hotel, hotels)
            all_plans.append(plan)
            if getattr(client, "stop_reason", None):
                stopped = True
                break
        if stopped:
            break
    # 景点只为最终候选补充，不让可选查询优先耗尽核心交通预算。
    preliminary = rank_plans(all_plans, req)[:max_plans]
    for plan in preliminary:
        if getattr(client, "stop_reason", None):
            break
        pois, prov = query_pois(client, plan.city, cached=cached)
        plan.pois = pois[:5]
        plan.poi_query = prov
        plan.provenance.append(prov)
    outcome.all_plans = all_plans
    outcome.provenance = _dedup_provenance([p for plan in all_plans for p in plan.provenance])
    outcome.stats.update({
        "n_candidates": len(candidates),
        "n_queries_live": sum(p.get("source") == "live" for p in outcome.provenance),
        "n_queries_cached": sum(str(p.get("source", "")).startswith(("cache", "fixture")) for p in outcome.provenance),
        "n_client_live_calls": getattr(client, "live_calls", 0),
        "n_risk_control_hits": getattr(client, "risk_control_hits", 0),
        "time_optimal": time_optimal,
        "n_date_pairs_available": len(pairs),
        "n_date_pairs_selected": len(selected_pairs),
        "n_city_date_pairs_completed": len(all_plans),
        "nights": req.nights,
    })
    search_truncated = len(selected_pairs) < len(pairs) or len(all_plans) < len(pairs) * len(candidates)

    # ---- 5-6. 排序 ----
    ranked = rank_plans(all_plans, req)
    query_failed = any(not p.get("ok", True) for p in outcome.provenance)
    outcome.stats["search_scope"] = f"已计算{len(all_plans)}个城市日期组合；日期上限{max_date_pairs}组，仅基于接口返回班次，不保证全市场最优"
    outcome.stats["incomplete_search"] = query_failed or search_truncated
    if not ranked:
        outcome.status = "error" if query_failed else ("search_incomplete" if search_truncated else "no_solution")
        if query_failed:
            outcome.error = getattr(client, "stop_reason", None) or "部分查询失败，无法据此判定旅行条件无解"
        outcome.diagnosis = diagnose_infeasible(all_plans, req)
        return outcome

    outcome.plans = ranked[:max_plans]
    outcome.stats["n_feasible"] = len({plan.city for plan in ranked})
    outcome.stats["n_feasible_plans"] = len(ranked)
    outcome.stats["dep_date"] = outcome.plans[0].date_pair.dep_date.isoformat()
    outcome.stats["ret_date"] = outcome.plans[0].date_pair.ret_date.isoformat()
    outcome.stats["arrival_buffer_minutes"] = req.arrival_buffer_minutes
    outcome.stats["departure_buffer_minutes"] = req.departure_buffer_minutes
    if any(plan.confirmation_required for plan in outcome.plans) or outcome.stats["incomplete_search"]:
        outcome.status = "needs_confirmation"
    return outcome


def _preferred_poi(req: TripRequest) -> Optional[str]:
    """可选的酒店定位参考点。

    首版不臆测用户想去哪个景点，仅当请求里显式给出 hotel_poi 时才使用。
    没有可靠依据时返回 None，让接口按全城检索——不编造位置偏好。
    """
    return getattr(req, "hotel_poi", None)


def _room_candidates(hotels: List[HotelOption]) -> List[HotelOption]:
    """名称明确提示床位/共享住宿的结果不用于默认整房候选。

    名称筛选只是排除提示，不能证明剩余候选具备双人房或库存。
    用户明确核实的整房报价由上游单独采用，不受此名称启发式影响。
    """
    shared_markers = ("青年旅社", "青年旅舍", "青年公寓", "青年旅馆", "青旅",
                      "床位", "胶囊", "太空舱", "背包客", "多人间", "宿舍",
                      "hostel", "dormitory", "capsule", "youth apartment")
    pool = [h for h in hotels if h.quote_confirmed or not any(
        word in h.name.casefold() for word in shared_markers)]
    return sorted(pool, key=lambda h: (h.quote_confirmed, h.rate is not None,
                                      h.rate if h.rate is not None else -1), reverse=True)


def _pick_hotel(hotels: List[HotelOption], req: TripRequest) -> Optional[HotelOption]:
    """排除明确共享住宿提示，按返回评分展示待核实整房候选。"""
    pool = _room_candidates(hotels)
    return pool[0] if pool else None


def describe_hotel_choice(plan_hotel: Optional[HotelOption], hotels: List[HotelOption]) -> str:
    """说明酒店是怎么被选出来的，保证选择依据透明。"""
    if plan_hotel is None:
        return "返回结果为空或均含青旅、青年公寓、床位等共享住宿提示；请补查整房住宿，不以床位价代替"
    if not plan_hotel.quote_confirmed:
        return "已排除名称含青旅、青年公寓、床位等共享住宿提示的结果，再按评分展示；名称筛选不能证明房型适合，须核实整房、人数、位置及库存；展示价不参与预算"
    return f"采用用户明确确认的整房每晚报价，来源：{plan_hotel.quote_source}；库存和最终结算仍待核实"
