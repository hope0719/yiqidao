"""把规划结果渲染成 Markdown。

严格遵守需求"六、输出格式"：
  * 先简短推荐理由，再对比表
  * 每人往返交通及时间、每人已知费用与待核实费用、共同停留时间、
    酒店候选与住宿分摊、主要取舍与未核实事项、查询时间与预订链接
  * 分别标注"已知花费较低""交通负担更均衡""共同停留时间较长"
  * 只有一两个可行方案时不硬凑三个
  * 无可行方案时指出导致无解的条件并给出放宽方式，不静默忽略约束

真实性纪律（写进输出文本里，不只是代码注释）：
  * 共同停留时间 != 实际游玩时间，市内交通/接驳/入住/休息单独说明为未核实
  * 价格缺失显示"待核实"，绝不显示 0 元
  * 无余票字段 -> 明确写"库存未确认，以预订页为准"
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from planner import STRATEGY_LABEL, CityPlan, MemberPlan
from orchestrator import PlanOutcome
from tripmodel import TripRequest
from connection import connection_notes

UNVERIFIED_NOTE = (
    "共同停留时间只表示\"人都已到齐\"的区间，**不等于实际游玩时间**。"
    "已按请求扣除固定接驳/离站缓冲；具体市内交通、办理入住与休息仍需核实，"
    "飞猪接口未提供这些数据，一律标记为**未核实**。"
)

STOCK_NOTE = (
    "接口未返回余票/库存字段，**库存未确认**；"
    "最终价格与可订状态以预订页面为准。"
)


def fmt_money(v: Optional[float], missing: bool = False) -> str:
    if v is None or missing:
        return "待核实"
    return f"¥{v:,.2f}"


def fmt_minutes(v: Optional[int]) -> str:
    if v is None:
        return "—"
    h, m = divmod(int(v), 60)
    if h and m:
        return f"{h}小时{m}分"
    if h:
        return f"{h}小时"
    return f"{m}分"


def fmt_dt(v: Optional[datetime]) -> str:
    if v is None:
        return "—"
    return f"{v:%m-%d %H:%M}"


def _mode_label(opt) -> str:
    return {"flight": "飞机", "train": "火车"}.get(opt.mode, opt.mode)


def render_member_row(p: MemberPlan) -> Dict[str, str]:
    """一位成员在对比表中的一行。"""
    if p.outbound is None and p.ret is None and p.transport_known == 0.0:
        return {
            "member": p.name,
            "outbound": "本地（无需长途交通）",
            "ret": "本地",
            "known": fmt_money(0),
            "pending": "市内交通、餐饮、门票",
        }

    ob = p.outbound
    rt = p.ret
    ob_txt = (
        f"{_mode_label(ob)} {ob.transport_summary} "
        f"{fmt_dt(ob.dep_time)}→{fmt_dt(ob.arr_time)} "
        f"{fmt_minutes(ob.total_duration_min)} {fmt_money(ob.price)}"
        if ob else "无可行班次"
    )
    rt_txt = (
        f"{_mode_label(rt)} {rt.transport_summary} "
        f"{fmt_dt(rt.dep_time)}→{fmt_dt(rt.arr_time)} "
        f"{fmt_minutes(rt.total_duration_min)} {fmt_money(rt.price)}"
        if rt else "无可行班次"
    )
    for label, opt in (("去程", ob), ("返程", rt)):
        if opt and len(opt.segments) > 1:
            details = []
            for left, right in zip(opt.segments, opt.segments[1:]):
                gap = ((right.dep_time - left.arr_time).total_seconds() / 60) if left.arr_time and right.dep_time else None
                details.append(f"{left.arr_station or '站点未知'}→{right.dep_station or '站点未知'}，间隔{f'{gap:g}分钟' if gap is not None else '待核实'}")
            extra = "；".join(details) + "（换乘及联程保障待核实）"
            if label == "去程":
                ob_txt += "；" + extra
            else:
                rt_txt += "；" + extra
    pending_parts: List[str] = []
    if p.transport_missing:
        pending_parts.append("部分交通票价缺失")
    if p.lodging_missing:
        pending_parts.append("住宿")
    pending_parts.append("市内交通/接驳、餐饮、门票")
    return {
        "member": p.name,
        "outbound": ob_txt,
        "ret": rt_txt,
        "known": fmt_money(p.known_cost),
        "pending": "、".join(pending_parts),
    }


def render_plan(plan: CityPlan, req: TripRequest, rank: int) -> str:
    """渲染单个方案。"""
    lines: List[str] = []
    badge = STRATEGY_LABEL.get(plan.strategy, plan.strategy)
    lines.append(f"### 方案 {rank}：会合于 **{plan.city}**（{badge}）")
    lines.append("")
    lines.append(
        f"- 行程：{plan.date_pair.dep_date} 出发 → {plan.date_pair.ret_date} 返程"
        f"（{plan.date_pair.nights} 晚）"
    )
    lines.append(
        f"- 共同停留：**{plan.common_hours} 小时**"
        f"（{fmt_dt(plan.common_start)} ~ {fmt_dt(plan.common_end)}）"
    )
    worst = (
        f"{plan.worst_burden_member}（往返 {fmt_minutes(plan.worst_burden_minutes)}）"
        if plan.worst_burden_member else "—"
    )
    lines.append(f"- 最辛苦成员：{worst}")
    team_cost = fmt_money(plan.team_known_cost)
    suffix = "（**不含待核实项**）" if plan.team_cost_incomplete else ""
    lines.append(f"- 团队已知费用小计：**{team_cost}**{suffix}")
    lines.append("")

    # 每人明细表
    lines.append("| 成员 | 去程 | 返程 | 已知费用 | 待核实费用 |")
    lines.append("|---|---|---|---|---|")
    for mp in plan.members:
        row = render_member_row(mp)
        lines.append(
            f"| {row['member']} | {row['outbound']} | {row['ret']} "
            f"| {row['known']} | {row['pending']} |"
        )
    lines.append("")

    # 酒店
    if plan.chosen_hotel:
        h = plan.chosen_hotel
        rooms = req.rooming.rooms if req.rooming else 1
        nights = plan.date_pair.nights
        total = (h.price_per_night * nights * rooms) if h.quote_confirmed and h.price_per_night is not None else None
        per_member = (total / req.n_members) if total is not None else None
        lines.append("**酒店候选与住宿分摊**")
        lines.append("")
        lines.append(f"- {h.name}（{h.star or '档次未标注'}{f'，评分 {h.rate:.1f}' if h.rate else '，评分未提供'}）")
        if h.quote_confirmed:
            lines.append(f"- 已确认整房每晚报价 {fmt_money(h.price_per_night)} × {nights} 晚 × {rooms} 间 = 住宿参考额 **{fmt_money(total)}**")
            lines.append(f"- 按 {req.n_members} 人均摊，每人 **{fmt_money(per_member)}**；报价来源：{h.quote_source}")
        else:
            lines.append(f"- 搜索展示价 {fmt_money(h.display_price)}（可能为起价/床位价，**不计入住宿与预算**）")
            lines.append("- 请补充：酒店与房型、入住/退房日期、每房每晚含税价、每房可住人数、报价来源；如深夜到店，再确认夜间入住和房间保留。住宿分摊暂不计算。")
        if h.address or h.nearby:
            lines.append(f"- 位置：{h.nearby or ''} {h.address or ''}".rstrip())
        lines.append(f"- 预订链接：{h.detail_url or '（未提供）'}")
        if plan.hotel_choice_rationale:
            lines.append(f"- 选择依据：{plan.hotel_choice_rationale}")
        lines.append("")
        # 备选酒店
        others = [x for x in plan.hotels if x.name != h.name][:3]
        if others:
            lines.append("备选酒店：")
            lines.append("")
            lines.append("| 酒店 | 搜索展示价（未确认） | 档次 | 评分 | 链接 |")
            lines.append("|---|---|---|---|---|")
            for o in others:
                lines.append(
                    f"| {o.name} | {fmt_money(o.display_price)} | {o.star or '—'} "
                    f"| {f'{o.rate:.1f}' if o.rate else '—'} | [预订]({o.detail_url or ''}) |"
                )
            lines.append("")
    else:
        lines.append("**酒店候选**：暂无适合默认整房需求的候选，住宿费用待核实。")
        if plan.hotel_choice_rationale:
            lines.append(f"- {plan.hotel_choice_rationale}")
        lines.append("")

    if plan.confirmation_required:
        lines.append("**需要确认后再预订**")
        lines.append("")
        lines.extend(f"- {item}" for item in plan.confirmation_required)
        lines.append("")

    # 活动建议
    if plan.pois:
        lines.append("**少量活动建议**")
        lines.append("")
        for poi in plan.pois[:4]:
            price = (
                "接口标示免费（待预订页核实）" if poi.free_status == "FREE"
                else (fmt_money(poi.ticket_price) if poi.ticket_price is not None else "票价待核实")
            )
            tag = f"（{poi.category}）" if poi.category else ""
            lines.append(f"- {poi.name}{tag} — {price}")
            if poi.jump_url:
                lines.append(f"  [详情/预订]({poi.jump_url})")
        lines.append("")

    # 取舍与未核实
    lines.append("**主要取舍与未核实事项**")
    lines.append("")
    tradeoffs = _tradeoffs(plan, req)
    for t in tradeoffs:
        lines.append(f"- {t}")
    lines.append("")

    # 预订链接汇总
    lines.append("**预订链接**")
    lines.append("")
    for mp in plan.members:
        if mp.outbound and mp.outbound.jump_url:
            lines.append(f"- {mp.name} 去程：{mp.outbound.jump_url}")
        if mp.ret and mp.ret.jump_url:
            lines.append(f"- {mp.name} 返程：{mp.ret.jump_url}")
    lines.append("")
    return "\n".join(lines)


def _tradeoffs(plan: CityPlan, req: TripRequest) -> List[str]:
    """由代码归纳该方案的取舍点，避免模型空泛描述。"""
    out: List[str] = []
    if plan.strategy == "cheapest":
        out.append("优先选择未识别到紧张或未知换乘风险的组合，再比较已知费用；仍需核实实际可衔接性。")
    elif plan.strategy == "max_common":
        out.append("优先拉长共同停留时间，交通花费可能高于最省钱方案。")
    elif plan.strategy == "min_burden":
        out.append("优先降低最辛苦成员的交通负担，花费可能不是最低。")

    if plan.chosen_hotel is None or not plan.chosen_hotel.quote_confirmed:
        out.append("住宿未确认，当前城市排序主要比较已知交通费用，确认住宿后可能改变顺序。")
    if plan.team_cost_incomplete:
        out.append("**费用不完整**：存在缺失价格项，已知小计不代表总支出，"
                   "不能据此断定满足预算。")

    burdens = [(p.total_minutes, p.name) for p in plan.members if p.total_minutes]
    if len(burdens) >= 2:
        mx = max(burdens, key=lambda x: x[0])
        mn = min(burdens, key=lambda x: x[0])
        if mx[0] and mn[0] and mx[0] - mn[0] >= 120:
            out.append(
                f"交通负担不均衡：{mx[1]} 往返 {fmt_minutes(mx[0])}，"
                f"{mn[1]} 仅 {fmt_minutes(mn[0])}。"
            )

    over = [p for p in plan.members if p.budget_state == "over"]
    for p in over:
        out.append(f"{p.name} 已知费用已超其个人预算 {fmt_money(abs(p.budget_delta or 0))}。")

    unknown_budget = [p for p in plan.members if p.budget_state == "unknown"]
    if unknown_budget:
        out.append(
            f"{'、'.join(p.name for p in unknown_budget)} 的费用不完整，"
            f"是否在个人预算内**无法确认**。"
        )

    # 部分查询失败但方案仍可行时，必须披露，不能让用户以为数据是完整的
    if plan.query_notes:
        uniq = []
        for n in plan.query_notes:
            if n not in uniq:
                uniq.append(n)
        out.append(
            "**部分查询失败**（不影响本方案成立，但覆盖面不完整）："
            + "；".join(uniq[:4])
            + ("；等" if len(uniq) > 4 else "")
        )

    out.append(UNVERIFIED_NOTE)
    out.append(STOCK_NOTE)
    return out


def render_outcome(outcome: PlanOutcome, req: TripRequest) -> str:
    """渲染完整输出。"""
    lines: List[str] = []
    lines.append("# 一起到 · 多人异地会合方案")
    lines.append("")
    lines.append(f"查询时间：{outcome.generated_at}")
    if outcome.stats:
        s = outcome.stats
        lines.append(
            f"候选城市：{s.get('n_candidates', 0)} 个 · "
            f"实时查询 {s.get('n_queries_live', 0)} 次 · "
            f"命中缓存 {s.get('n_queries_cached', 0)} 次"
        )
    lines.append("")

    if outcome.stats.get("search_scope"):
        lines.append("> " + outcome.stats["search_scope"])
        lines.append("")
    if outcome.stats.get("incomplete_search"):
        lines.append("> 部分查询失败或候选未查完，以下结果仅供参考，不能判定全局最优。")
        lines.append("")

    if any(p.get("raw_message") == "SYNTHETIC TEST DATA" for p in outcome.provenance):
        lines.append("> **虚构测试数据：以下班次、酒店和价格不可预订，不代表真实查询。**")
        lines.append("")

    # 需要补问
    if outcome.status == "needs_input":
        lines.append("## 需要补充信息")
        lines.append("")
        lines.append("以下条件影响可行性判断，请先明确：")
        lines.append("")
        for q in outcome.questions:
            lines.append(f"- {q}")
        lines.append("")
        for i in outcome.issues:
            if not i.get("blocking") and i.get("message"):
                lines.append(f"> 提示：{i['message']}")
        return "\n".join(lines)

    if outcome.status == "search_incomplete":
        lines.append("## 搜索尚未完成，不能判定无解")
        lines.append("")
        lines.append("本轮达到日期搜索上限，还有日期或城市未查。可增大--max-date-pairs，或指定一组日期后继续。")
        return "\n".join(lines)

    # 无解
    if outcome.status == "no_solution":
        lines.append("## 在已查询范围内没有找到可行方案")
        lines.append("")
        diag = outcome.diagnosis or {}
        if diag.get("reason"):
            lines.append(f"**原因**：{diag['reason']}")
            lines.append("")
        blockers = diag.get("blockers") or []
        if blockers:
            lines.append("**本轮未找到组合的原因**：")
            lines.append("")
            for b in blockers:
                reason = b.get("reason") if isinstance(b, dict) else str(b)
                count = b.get("count") if isinstance(b, dict) else None
                suffix = f"（{count} 个城市）" if count else ""
                lines.append(f"- {reason}{suffix}")
            lines.append("")
        if diag.get("suggestions"):
            lines.append("**可选择的放宽方式**：")
            lines.append("")
            for s in diag["suggestions"]:
                lines.append(f"- {s}")
            lines.append("")
        if diag.get("cities_tried"):
            lines.append(f"已尝试城市：{'、'.join(diag['cities_tried'])}")
            lines.append("")
        lines.append(f"> {UNVERIFIED_NOTE}")
        return "\n".join(lines)

    if outcome.status == "error":
        lines.append("## 查询出错")
        lines.append("")
        lines.append(str(outcome.error or "未知错误"))
        return "\n".join(lines)

    if outcome.status == "needs_confirmation":
        lines.append("## 已有候选方案，仍有待确认事项或未完成的搜索")
        lines.append("")
        lines.append("请检查方案待确认事项和搜索覆盖范围。酒店展示价不作为整房价格，已知小计不代表旅行总价。")
        lines.append("")

    if "arrival_buffer_minutes" in outcome.stats:
        lines.append(f"> 共同停留已扣除到站后 {outcome.stats['arrival_buffer_minutes']} 分钟与离站前 {outcome.stats['departure_buffer_minutes']} 分钟的缓冲；仍包含睡眠和休息，不等于游玩时长。")
        lines.append("")

    # 正常输出
    plans = outcome.plans
    if not plans:
        lines.append("没有可行方案。")
        return "\n".join(lines)

    top = plans[0]
    lines.append("## 推荐理由")
    lines.append("")
    lines.append(
        f"在 {outcome.stats.get('n_candidates', 0)} 个候选城市中，"
        f"共 {outcome.stats.get('n_feasible', len(plans))} 个未发现已知约束冲突，缺失费用与实际可订性仍待核实。"
        f"综合推荐 **{top.city}**：团队已知费用小计 "
        f"{fmt_money(top.team_known_cost)}"
        f"{'（不含待核实项）' if top.team_cost_incomplete else ''}，"
        f"共同停留 {top.common_hours} 小时，"
        f"最辛苦成员为 {top.worst_burden_member or '—'}"
        f"（往返 {fmt_minutes(top.worst_burden_minutes)}）。"
    )
    lines.append("")
    lines.append(f"行程日期：{outcome.stats.get('dep_date')} → {outcome.stats.get('ret_date')}。")
    lines.append("")

    # 总览对比表
    lines.append("## 方案对比")
    lines.append("")
    lines.append("| 方案 | 目的地 | 共同停留 | 团队已知费用 | 最辛苦成员 | 特点 |")
    lines.append("|---|---|---|---|---|---|")
    for i, p in enumerate(plans, 1):
        lines.append(
            f"| {i} | {p.city}（{p.date_pair.dep_date}→{p.date_pair.ret_date}） | {p.common_hours} 小时 | {fmt_money(p.team_known_cost)}"
            f"{'*' if p.team_cost_incomplete else ''} "
            f"| {p.worst_burden_member or '—'}（{fmt_minutes(p.worst_burden_minutes)}） "
            f"| {STRATEGY_LABEL.get(p.strategy, p.strategy)} |"
        )
    lines.append("")
    if any(p.team_cost_incomplete for p in plans):
        lines.append("> \\* 含缺失价格项，已知小计不代表总支出。")
        lines.append("")

    # 各方案详情
    lines.append("## 方案详情")
    lines.append("")
    for i, p in enumerate(plans, 1):
        lines.append(render_plan(p, req, i))

    # 数据来源
    lines.append("## 数据来源与查询时间")
    lines.append("")
    lines.append("| 端点 | 查询参数 | 结果 | 时间 | 来源 |")
    lines.append("|---|---|---|---|---|")
    shown = outcome.provenance[:30]
    for prov in shown:
        params = prov.get("params") or {}
        brief = ", ".join(f"{k}={v}" for k, v in list(params.items())[:4])
        if len(brief) > 60:
            brief = brief[:57] + "..."
        state = "成功" if prov.get("ok") else f"失败:{prov.get('failure_kind')}"
        if prov.get("empty"):
            state = "空结果"
        lines.append(
            f"| {prov.get('endpoint')} | {brief} | {state} "
            f"| {prov.get('queried_at')} | {prov.get('source')} |"
        )
    lines.append("")
    lines.append(f"> {STOCK_NOTE}")
    lines.append("")
    sources = {str(p.get("source", "")) for p in outcome.provenance}
    if any(p.get("raw_message") == "SYNTHETIC TEST DATA" for p in outcome.provenance):
        lines.append("数据来源：虚构测试数据回放，不代表真实报价。")
    elif sources and all(src.startswith("fixture") for src in sources):
        lines.append("数据来源：历史响应离线回放，不是本轮实时查询。")
    elif any(src.startswith("cache") for src in sources):
        lines.append("数据来源：包含历史缓存，逐条来源与原查询时间见表格。")
    elif "live" in sources:
        lines.append("数据来源：本轮飞猪FlyAI查询；库存与最终结算仍未确认。")
    else:
        lines.append("数据来源未核实。")
    return "\n".join(lines)
