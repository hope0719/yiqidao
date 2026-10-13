"""在已返回班次内搜索往返组合。以共同开始时刻扫描，避免全员笛卡尔积爆炸。

每位成员先枚举往返对并校验个人预算/时长；扫描所有可能的最晚到达时刻，
筛选可覆盖最少共同停留的往返对。对于每个成员按换乘风险与缺价分组保留最低费用方案，
比较分类组合，优先较少换乘风险成员，再比较完整价格和已知费用。
范围仅限传入班次及一个日期对；不保证全市场最优。
"""
from dataclasses import replace
from datetime import timedelta
from itertools import product
from planner import build_member_plan, build_city_plan
from connection import member_connection_risk


def solve_city(client, req, city, pair, legs_by_member, hotel, cached=True):
    choices = []
    for member in req.members:
        legs = legs_by_member[member.name]
        outbound, ret = legs['outbound'], legs['return']
        local = not outbound.options and not outbound.provenance and not outbound.query_failures
        if local:
            options = [build_member_plan(member, legs, 'cheapest', hotel, req.rooming, req.n_members, 'equal_per_member')]
        else:
            options = []
            for ob, back in product(outbound.feasible, ret.feasible):
                selected = {'outbound': replace(outbound, feasible=[ob]), 'return': replace(ret, feasible=[back])}
                plan = build_member_plan(member, selected, 'cheapest', hotel, req.rooming, req.n_members, 'equal_per_member')
                if not plan.violations and plan.arrive_at and plan.depart_at and plan.depart_at > plan.arrive_at:
                    options.append(plan)
        if not options:
            fallback = build_city_plan(client, req, city, pair, legs_by_member, 'cheapest', hotel, cached=cached)
            fallback.feasible = False
            fallback.blockers.append(f'{member.name} 在返回班次中没有满足个人约束的往返组合')
            return fallback
        choices.append(options)

    thresholds = sorted({p.arrive_at for options in choices for p in options if p.arrive_at})
    if not thresholds:
        return build_city_plan(client, req, city, pair, legs_by_member, 'cheapest', hotel, cached=cached)
    best = None
    best_key = None
    for latest_arrival in thresholds:
        departure_floor = latest_arrival + timedelta(minutes=req.arrival_buffer_minutes + req.departure_buffer_minutes, hours=req.min_common_hours)
        groups = []
        for options in choices:
            eligible = [p for p in options if p.arrive_at is None or (p.arrive_at <= latest_arrival and p.depart_at >= departure_floor)]
            if not eligible:
                break
            representatives = []
            for missing, risk in product((False, True), (0, 1)):
                category = [p for p in eligible if p.transport_missing == missing and member_connection_risk(p) == risk]
                if category:
                    # 同价优先晚离开和短时长。最晚到达阈值会在其他扫描轮次覆盖。
                    representatives.append(min(category, key=lambda p: (
                        p.known_cost if p.known_cost is not None else 0,
                        -(p.depart_at.timestamp()) if p.depart_at else 0,
                        p.total_minutes if p.total_minutes is not None else 10**9,
                    )))
            groups.append(representatives)
        if len(groups) != len(choices):
            continue
        for combination in product(*groups):
            cost = round(sum(p.known_cost or 0 for p in combination), 2)
            if req.team_budget is not None and cost > req.team_budget:
                continue
            key = (sum(member_connection_risk(p) for p in combination), any(p.transport_missing for p in combination), cost)
            if best_key is not None and key > best_key[:3]:
                continue
            candidate = build_city_plan(client, req, city, pair, legs_by_member, 'cheapest', hotel,
                                        cached=cached, selected_members=list(combination))
            if not candidate.feasible:
                continue
            full_key = (*key, -(candidate.common_hours or 0), candidate.worst_burden_minutes or 0)
            if best_key is None or full_key < best_key:
                best, best_key = candidate, full_key
    if best is not None:
        return best
    fallback = build_city_plan(client, req, city, pair, legs_by_member, 'cheapest', hotel, cached=cached)
    fallback.feasible = False
    fallback.blockers.append('在已返回班次中没有找到同时满足共同停留、个人和团队预算的往返组合')
    return fallback
