"""换乘优先级、逐段间隔与深夜入住回归；全部为构造数据。"""
import unittest
from dataclasses import replace
from datetime import timedelta
from test_search_logic import option, legs, req, PAIR
from tripmodel import Member
from connection import connection_notes
from solver import solve_city
from render import render_member_row


def connecting(direction='return', gap=17, station='常州', next_station='常州'):
    base = option(direction, 20, 180, 9)
    first = replace(base.segments[0], arr_station=station,
                    arr_time=base.dep_time + timedelta(minutes=60))
    second = replace(base.segments[0], dep_station=next_station,
                     dep_time=first.arr_time + timedelta(minutes=gap))
    return replace(base, mode='train', segments=[first, second], transfer_duration_min=999)


class ConnectionTests(unittest.TestCase):
    def test_segment_clock_overrides_aggregate_transfer_field(self):
        notes = connection_notes(connecting())
        self.assertTrue(notes)
        self.assertIn('17分钟', notes[0])
        self.assertFalse(connection_notes(connecting(gap=30)))

    def test_cross_station_and_unknown_stations_require_confirmation(self):
        for station, next_station in [('常州', '常州北'), (None, None)]:
            self.assertTrue(connection_notes(connecting(gap=90, station=station, next_station=next_station)))

    def test_solver_prefers_roomier_option_but_preserves_budget_constraints(self):
        members = [Member('A','北京',budget=500), Member('B','上海',budget=500)]
        request = req(members)
        ob = option('outbound', 50, 60, 9)
        risky = connecting()
        safer = option('return', 80, 180, 9)
        data = {'A': legs([ob], [risky, safer]), 'B': legs([ob], [safer])}
        plan = solve_city(None,request,'杭州',PAIR,data,None)
        self.assertEqual(plan.members[0].ret.price,80)
        members[0].budget = 90
        fallback = solve_city(None,request,'杭州',PAIR,data,None)
        self.assertTrue(fallback.feasible)
        self.assertEqual(fallback.members[0].ret.price,20)
        self.assertTrue(any('17分钟' in n for n in fallback.confirmation_required))
        self.assertIn('17分钟',render_member_row(fallback.members[0])['ret'])

    def test_late_same_day_arrival_requires_hotel_confirmation(self):
        request = req([Member('A','北京',budget=500),Member('B','上海',budget=500)])
        ob=option('outbound',50,60,23);rt=option('return',50,60,23)
        plan=solve_city(None,request,'杭州',PAIR,{m.name:legs([ob],[rt]) for m in request.members},None)
        self.assertTrue(any('深夜' in n for n in plan.confirmation_required))
