"""计算与校验层测试。全部使用固定测试数据，不联网。

覆盖需求"八、验证"中可离线验证的场景：
  * 预算/时间条件无解
  * 不同成员到达时间不一致 -> 共同停留时间
  * 价格字段缺失 -> 不得当作满足预算
  * 住宿分摊与房间数
  * 请求校验（含不静默忽略用户约束）
"""

import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from normalize import TransportOption  # noqa: E402
from planner import (  # noqa: E402
    DatePair,
    MemberPlan,
    build_member_plan,
    compute_common_stay,
    compute_lodging_share,
    compute_lodging_total,
    select_date_pairs,
)
from normalize import HotelOption  # noqa: E402
from tripmodel import (  # noqa: E402
    Member,
    RoomingPlan,
    TripRequest,
    blocking_issues,
    questions_for_user,
    validate_request,
)

TODAY = date(2026, 10, 10)


def make_opt(dep, arr, price, minutes, mode="flight", direction="outbound"):
    """构造一个交通候选（测试数据）。"""
    return TransportOption(
        mode=mode,
        direction=direction,
        price=price,
        price_is_missing=price is None,
        total_duration_min=minutes,
        journey_type="直达",
        segments=[_FakeSeg(dep, arr)],
        jump_url="https://example.test/x",
    )


class _FakeSeg:
    def __init__(self, dep, arr):
        self.dep_time = dep
        self.arr_time = arr
        self.dep_station = "测试站"
        self.arr_station = "测试站"
        self.transport_no = "CA1"
        self.transport_name = "测试航司"
        self.duration_min = None


class _FakeLeg:
    """模拟 MemberLeg 的最小接口。"""
    def __init__(self, feasible, failures=None, options=None):
        self.feasible = feasible
        self.options = options if options is not None else feasible
        self.query_failures = failures or []
        self.provenance = []
        self.reject_reasons = []


class TestCommonStay(unittest.TestCase):
    def test_latest_arrival_to_earliest_departure(self):
        """共同停留 = 最晚到达 -> 最早离开。"""
        plans = [
            MemberPlan(name="A", origin="北京", arrive_at=datetime(2026, 10, 20, 10, 0),
                       depart_at=datetime(2026, 10, 22, 18, 0)),
            MemberPlan(name="B", origin="上海", arrive_at=datetime(2026, 10, 20, 14, 30),
                       depart_at=datetime(2026, 10, 22, 20, 0)),
            MemberPlan(name="C", origin="广州", arrive_at=datetime(2026, 10, 20, 9, 0),
                       depart_at=datetime(2026, 10, 22, 16, 0)),
        ]
        start, end, hours = compute_common_stay(plans)
        self.assertEqual(start, datetime(2026, 10, 20, 14, 30))  # B 最晚到
        self.assertEqual(end, datetime(2026, 10, 22, 16, 0))     # C 最早走
        # 14:30 -> 次日16:00 是 25.5h，再到次日16:00 共 49.5h
        self.assertEqual(hours, 49.5)

    def test_no_overlap_gives_zero(self):
        """有人还没到就有人走了 -> 0 小时，不是负数。"""
        plans = [
            MemberPlan(name="A", origin="X", arrive_at=datetime(2026, 10, 20, 18, 0),
                       depart_at=datetime(2026, 10, 22, 9, 0)),
            MemberPlan(name="B", origin="Y", arrive_at=datetime(2026, 10, 20, 8, 0),
                       depart_at=datetime(2026, 10, 20, 12, 0)),
        ]
        _, _, hours = compute_common_stay(plans)
        self.assertEqual(hours, 0.0)

    def test_local_member_ignored(self):
        """本地成员无交通时刻，不得把区间压成空。"""
        plans = [
            MemberPlan(name="本地", origin="杭州", arrive_at=None, depart_at=None),
            MemberPlan(name="A", origin="北京", arrive_at=datetime(2026, 10, 20, 10, 0),
                       depart_at=datetime(2026, 10, 22, 18, 0)),
        ]
        start, end, hours = compute_common_stay(plans)
        self.assertEqual(start, datetime(2026, 10, 20, 10, 0))
        self.assertEqual(hours, 56.0)

    def test_missing_times_gives_none(self):
        plans = [MemberPlan(name="A", origin="X", arrive_at=None, depart_at=None)]
        self.assertEqual(compute_common_stay(plans), (None, None, None))


class TestLodging(unittest.TestCase):
    def setUp(self):
        self.hotel = HotelOption(
            name="测试酒店", price_per_night=300.0, price_is_missing=False,
            star="舒适型", rate=4.5, address="", nearby="", detail_url="",
            main_pic="", hotel_id="1", quote_confirmed=True, max_guests_per_room=2,
        )
        self.rooming = RoomingPlan(
            rooms=2, nights=2,
            assignments={"A": "R1", "B": "R1", "C": "R2"},
        )

    def test_total_is_per_night_times_nights_times_rooms(self):
        """住宿总额 = 每晚单价 × 晚数 × 房间数（酒店价是每晚单价）。"""
        total, missing = compute_lodging_total(self.hotel, self.rooming)
        self.assertEqual(total, 1200.0)   # 300 × 2 × 2
        self.assertFalse(missing)

    def test_missing_price_not_zero(self):
        """价格缺失时总额必须是 None，不能是 0。"""
        h = HotelOption(name="X", price_per_night=None, price_is_missing=True,
                        star=None, rate=None, address=None, nearby=None,
                        detail_url=None, main_pic=None, hotel_id=None)
        total, missing = compute_lodging_total(h, self.rooming)
        self.assertIsNone(total)
        self.assertTrue(missing)

    def test_equal_split(self):
        m = Member(name="A", origin_city="北京")
        share, missing, note = compute_lodging_share(
            self.hotel, self.rooming, m, n_members=3, split_method="equal_per_member")
        self.assertEqual(share, 400.0)   # 1200 / 3
        self.assertFalse(missing)

    def test_per_room_split(self):
        """R1 住 2 人、R2 住 1 人时，按房间分摊结果不同。"""
        a = Member(name="A", origin_city="北京")
        c = Member(name="C", origin_city="广州")
        share_a, _, _ = compute_lodging_share(
            self.hotel, self.rooming, a, 3, "per_room")
        share_c, _, _ = compute_lodging_share(
            self.hotel, self.rooming, c, 3, "per_room")
        self.assertEqual(share_a, 300.0)   # 每房 600，R1 两人 -> 300
        self.assertEqual(share_c, 600.0)   # R2 一人 -> 600
        self.assertNotEqual(share_a, share_c)

    def test_three_members_not_one_room(self):
        """3 人 2 间房：绝不能按 1 间房算总价。"""
        one_room = RoomingPlan(rooms=1, nights=2)
        total_one, _ = compute_lodging_total(self.hotel, one_room)
        total_two, _ = compute_lodging_total(self.hotel, self.rooming)
        self.assertEqual(total_one, 600.0)
        self.assertEqual(total_two, 1200.0)


class TestBudgetRules(unittest.TestCase):
    def setUp(self):
        self.rooming = RoomingPlan(rooms=1, nights=2, assignments={"A": "R1", "B": "R1"})
        self.hotel = HotelOption(name="H", price_per_night=200.0, price_is_missing=False,
                                 star=None, rate=None, address=None, nearby=None,
                                 detail_url=None, main_pic=None, hotel_id="1", quote_confirmed=True, max_guests_per_room=2)
        self.pair = DatePair(date(2026, 10, 20), date(2026, 10, 22), 2)

    def _member(self, budget, **kw):
        return Member(name="A", origin_city="北京", budget=budget, **kw)

    def test_over_budget_is_violation(self):
        m = self._member(budget=500)
        ob = make_opt(datetime(2026, 10, 20, 8, 0), datetime(2026, 10, 20, 10, 0), 400.0, 120)
        rt = make_opt(datetime(2026, 10, 22, 18, 0), datetime(2026, 10, 22, 20, 0), 400.0, 120,
                      direction="return")
        legs = {"outbound": _FakeLeg([ob]), "return": _FakeLeg([rt])}
        p = build_member_plan(m, legs, "cheapest", self.hotel, self.rooming, 2, "equal_per_member")
        self.assertEqual(p.budget_state, "over")
        self.assertTrue(p.violations)
        self.assertFalse(p.feasible)

    def test_missing_price_means_budget_unknown(self):
        """价格缺失时不得宣称满足预算。"""
        m = self._member(budget=5000)
        ob = make_opt(datetime(2026, 10, 20, 8, 0), datetime(2026, 10, 20, 10, 0), None, 120)
        rt = make_opt(datetime(2026, 10, 22, 18, 0), datetime(2026, 10, 22, 20, 0), None, 120,
                      direction="return")
        legs = {"outbound": _FakeLeg([ob]), "return": _FakeLeg([rt])}
        p = build_member_plan(m, legs, "cheapest", self.hotel, self.rooming, 2, "equal_per_member")
        self.assertEqual(p.budget_state, "unknown")
        self.assertTrue(p.transport_missing)

    def test_within_budget_ok(self):
        m = self._member(budget=2000)
        ob = make_opt(datetime(2026, 10, 20, 8, 0), datetime(2026, 10, 20, 10, 0), 500.0, 120)
        rt = make_opt(datetime(2026, 10, 22, 18, 0), datetime(2026, 10, 22, 20, 0), 500.0, 120,
                      direction="return")
        legs = {"outbound": _FakeLeg([ob]), "return": _FakeLeg([rt])}
        p = build_member_plan(m, legs, "cheapest", self.hotel, self.rooming, 2, "equal_per_member")
        self.assertEqual(p.budget_state, "ok")
        # 500+500 交通 + 200 住宿分摊(400/2) = 1200
        self.assertEqual(p.known_cost, 1200.0)

    def test_roundtrip_duration_limit(self):
        """往返总时长上限单独校验。"""
        m = self._member(budget=None, max_roundtrip_minutes=200)
        ob = make_opt(datetime(2026, 10, 20, 8, 0), datetime(2026, 10, 20, 10, 0), 100.0, 150)
        rt = make_opt(datetime(2026, 10, 22, 18, 0), datetime(2026, 10, 22, 20, 0), 100.0, 150,
                      direction="return")
        legs = {"outbound": _FakeLeg([ob]), "return": _FakeLeg([rt])}
        p = build_member_plan(m, legs, "cheapest", self.hotel, self.rooming, 2, "equal_per_member")
        self.assertTrue(any("往返合计" in v for v in p.violations))

    def test_partial_failure_does_not_block_when_other_mode_ok(self):
        """某方式查询失败但另一方式有班次时，不应判为不可行。"""
        m = Member(name="A", origin_city="北京")
        ob = make_opt(datetime(2026, 10, 20, 8, 0), datetime(2026, 10, 20, 10, 0), 100.0, 120)
        rt = make_opt(datetime(2026, 10, 22, 18, 0), datetime(2026, 10, 22, 20, 0), 100.0, 120,
                      direction="return")
        legs = {
            "outbound": _FakeLeg([ob], failures=["飞机 X->Y: 接口返回失败（status=1）"]),
            "return": _FakeLeg([rt]),
        }
        p = build_member_plan(m, legs, "cheapest", self.hotel, self.rooming, 2, "equal_per_member")
        self.assertTrue(p.feasible, f"应可行，但违规={p.violations}")
        self.assertTrue(p.query_notes, "失败信息应被记录以供披露")

    def test_no_return_violates(self):
        """只查到去程、没有返程时，必须判为不可行。"""
        m = Member(name="A", origin_city="北京")
        ob = make_opt(datetime(2026, 10, 20, 8, 0), datetime(2026, 10, 20, 10, 0), 100.0, 120)
        legs = {"outbound": _FakeLeg([ob]), "return": _FakeLeg([])}
        p = build_member_plan(m, legs, "cheapest", self.hotel, self.rooming, 2, "equal_per_member")
        self.assertFalse(p.feasible)
        self.assertTrue(any("返程" in v for v in p.violations))


class TestDatePairs(unittest.TestCase):
    def test_intersection(self):
        req = TripRequest(
            members=[
                Member(name="A", origin_city="北京", dep_date_start=date(2026, 10, 20),
                       dep_date_end=date(2026, 10, 22)),
                Member(name="B", origin_city="上海", dep_date_start=date(2026, 10, 21),
                       dep_date_end=date(2026, 10, 25)),
            ],
            nights=2, today=TODAY,
        )
        pairs, blockers = select_date_pairs(req, TODAY)
        self.assertFalse(blockers)
        self.assertTrue(pairs)
        # A 的出发窗结束于 10-22，B 开始于 10-21 -> 交集 10-21~10-22
        self.assertEqual(pairs[0].dep_date, date(2026, 10, 21))

    def test_no_intersection_is_infeasible(self):
        req = TripRequest(
            members=[
                Member(name="A", origin_city="北京", dep_date_start=date(2026, 10, 20),
                       dep_date_end=date(2026, 10, 21)),
                Member(name="B", origin_city="上海", dep_date_start=date(2026, 11, 5),
                       dep_date_end=date(2026, 11, 6)),
            ],
            nights=2, today=TODAY,
        )
        pairs, blockers = select_date_pairs(req, TODAY)
        self.assertEqual(pairs, [])
        self.assertTrue(blockers)

    def test_past_date_clamped_to_today(self):
        req = TripRequest(
            members=[Member(name="A", origin_city="北京",
                            dep_date_start=date(2026, 10, 1),
                            dep_date_end=date(2026, 10, 5))],
            nights=1, today=TODAY,
        )
        pairs, _ = select_date_pairs(req, TODAY)
        for p in pairs:
            self.assertGreaterEqual(p.dep_date, TODAY)

    def test_return_window_filters_pairs(self):
        """返程窗必须能容纳 dep+nights。"""
        req = TripRequest(
            members=[Member(name="A", origin_city="北京",
                            dep_date_start=date(2026, 10, 20),
                            dep_date_end=date(2026, 10, 25),
                            ret_date_start=date(2026, 10, 24),
                            ret_date_end=date(2026, 10, 26))],
            nights=2, today=TODAY,
        )
        pairs, blockers = select_date_pairs(req, TODAY)
        self.assertFalse(blockers)
        for p in pairs:
            self.assertEqual((p.ret_date - p.dep_date).days, 2)
            self.assertGreaterEqual(p.ret_date, date(2026, 10, 24))
            self.assertLessEqual(p.ret_date, date(2026, 10, 26))


class TestValidation(unittest.TestCase):
    def test_missing_rooming_blocks(self):
        """房间数不明确必须补问，不默认一间房住所有人。"""
        req = TripRequest(
            members=[Member(name="A", origin_city="北京"), Member(name="B", origin_city="上海")],
            candidates=["杭州"], nights=2, today=TODAY,
        )
        issues = validate_request(req, TODAY)
        self.assertTrue(blocking_issues(issues))
        self.assertTrue(any("几间房" in q for q in questions_for_user(issues)))

    def test_unsupported_mode_not_silently_ignored(self):
        """用户指定的交通方式不被支持时必须报错，不能静默改成飞机+火车。"""
        req = TripRequest(
            members=[
                Member(name="A", origin_city="北京", modes=["地铁"]),
                Member(name="B", origin_city="上海"),
            ],
            candidates=["杭州"], nights=2, today=TODAY,
            rooming=RoomingPlan(rooms=1, nights=2, assignments={"A": "R1", "B": "R1"}),
        )
        issues = validate_request(req, TODAY)
        self.assertTrue(any("地铁" in i.message for i in issues))

    def test_negative_budget_blocked(self):
        req = TripRequest(
            members=[
                Member(name="A", origin_city="北京", budget=-100),
                Member(name="B", origin_city="上海"),
            ],
            candidates=["杭州"], nights=2, today=TODAY,
            rooming=RoomingPlan(rooms=1, nights=2, assignments={"A": "R1", "B": "R1"}),
        )
        issues = validate_request(req, TODAY)
        self.assertTrue(any("预算必须为正" in i.message for i in issues))

    def test_tighter_roundtrip_limit_is_not_a_contradiction(self):
        """两个约束都是上限，短程班次可以同时满足，不能阻塞请求。"""
        req = TripRequest(
            members=[
                Member(name="A", origin_city="北京", max_oneway_minutes=600,
                       max_roundtrip_minutes=300),
                Member(name="B", origin_city="上海"),
            ],
            candidates=["杭州"], nights=2, today=TODAY,
            rooming=RoomingPlan(rooms=1, nights=2, assignments={"A": "R1", "B": "R1"}),
        )
        issues = validate_request(req, TODAY)
        self.assertFalse(any(i.blocking and "单程" in i.message and "往返" in i.message for i in issues))

    def test_too_many_candidates(self):
        req = TripRequest(
            members=[Member(name="A", origin_city="北京"), Member(name="B", origin_city="上海")],
            candidates=["杭州", "厦门", "长沙", "成都", "西安", "青岛"],
            nights=2, today=TODAY,
            rooming=RoomingPlan(rooms=1, nights=2, assignments={"A": "R1", "B": "R1"}),
        )
        issues = validate_request(req, TODAY)
        self.assertTrue(any("最多比较" in i.message for i in issues))

    def test_valid_request_passes(self):
        req = TripRequest(
            members=[
                Member(name="A", origin_city="北京", dep_date_start=date(2026, 10, 20), dep_date_end=date(2026,10,20), ret_date_start=date(2026,10,22), ret_date_end=date(2026,10,22),
                       budget=2000),
                Member(name="B", origin_city="上海", dep_date_start=date(2026, 10, 20), dep_date_end=date(2026,10,20), ret_date_start=date(2026,10,22), ret_date_end=date(2026,10,22),
                       budget=2000),
            ],
            candidates=["杭州"], nights=2, today=TODAY,
            rooming=RoomingPlan(rooms=1, nights=2, assignments={"A": "R1", "B": "R1"}),
        )
        issues = validate_request(req, TODAY)
        self.assertEqual(blocking_issues(issues), [], [i.message for i in issues])


if __name__ == "__main__":
    unittest.main(verbosity=2)
