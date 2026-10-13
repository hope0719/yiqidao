"""酒店起价、房型容量和实际共处区间的回归测试；全部离线。"""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'scripts'))
from normalize import HotelOption, normalize_transport
from planner import MemberPlan, compute_common_stay, compute_lodging_total
from tripmodel import RoomingPlan, validate_request, blocking_issues
from request_io import request_from_dict, request_to_dict
from orchestrator import plan_trip
from flyai_client import FlyAIClient, QueryResult, _canonical_key
from render import render_outcome
ROOT=Path(__file__).resolve().parent.parent

def request_data():
    return json.loads((ROOT/'examples/offline_replay.json').read_text())

class QuoteSafetyTests(unittest.TestCase):
    def test_low_or_high_search_price_never_becomes_room_quote(self):
        for price in ('¥10','¥618'):
            hotel=HotelOption.from_raw({'name':'示例旅社','price':price,'quote_confirmed':True})
            self.assertIsNone(hotel.price_per_night)
            self.assertFalse(hotel.quote_confirmed)
            self.assertEqual(compute_lodging_total(hotel,RoomingPlan(1,2)),(None,True))

    def test_unconfirmed_hotel_only_counts_transport_and_requests_confirmation(self):
        data=request_data();data.pop('hotel_quote')
        data['arrival_buffer_minutes']=60;data['departure_buffer_minutes']=90
        req=request_from_dict(data)
        out=plan_trip(req,FlyAIClient(mode='fixture',fixture_dir=ROOT/'examples/fixtures'))
        self.assertEqual(out.status,'needs_confirmation')
        self.assertEqual(out.plans[0].team_known_cost,2400)
        self.assertEqual(out.plans[0].common_hours,51.5)
        self.assertTrue(out.plans[0].team_cost_incomplete)
        self.assertTrue(all(m.budget_state=='unknown' for m in out.plans[0].members))
        text=render_outcome(out,req)
        self.assertIn('不计入住宿与预算',text)
        self.assertNotIn('住宿总额 **¥1,200**',text)

    def test_confirmed_quote_dates_and_capacity_are_required(self):
        for patch in ({'confirmed':False},{'room_price_per_night':float('nan')},{'max_guests_per_room':1},{'check_out_date':'2026-10-21'},{'source':''}):
            data=request_data();data['hotel_quote'].update(patch)
            self.assertTrue(blocking_issues(validate_request(request_from_dict(data))))

    def test_price_confirmed_only_for_its_city_and_exact_dates(self):
        data=request_data();data['hotel_quote']['check_in_date']='2026-10-21';data['hotel_quote']['check_out_date']='2026-10-23'
        out=plan_trip(request_from_dict(data),FlyAIClient(mode='fixture',fixture_dir=ROOT/'examples/fixtures'))
        self.assertEqual(out.status,'needs_confirmation')
        self.assertIsNone(out.plans[0].members[0].lodging_share)

    def test_per_room_overcrowding_blocks_even_when_total_beds_fit(self):
        data=request_data();data['rooming']['assignments']={m['name']:'R1' for m in data['members']}
        self.assertTrue(blocking_issues(validate_request(request_from_dict(data))))

    def test_departure_and_arrival_buffers_change_feasibility(self):
        plans=[MemberPlan(name='A',origin='北京',arrive_at=datetime(2026,10,20,10),depart_at=datetime(2026,10,20,12))]
        self.assertEqual(compute_common_stay(plans,60,90)[2],0)

    def test_confirmed_quote_and_buffers_survive_round_trip(self):
        data=request_data();data['arrival_buffer_minutes']=75;data['departure_buffer_minutes']=100
        restored=request_from_dict(request_to_dict(request_from_dict(data)))
        self.assertEqual(restored.hotel_quote,data['hotel_quote'])
        self.assertEqual(restored.arrival_buffer_minutes,75)
        self.assertEqual(restored.departure_buffer_minutes,100)

    def test_copied_old_cache_is_refreshed_despite_new_file_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/_canonical_key('search-flight',{})
            path.write_text(json.dumps({'endpoint':'search-flight','params':{},'ok':True,'items':[],'recorded_at':'2020-01-01T00:00:00+00:00'}))
            client=FlyAIClient(binary=sys.executable,fixture_dir=Path(td),mode='live')
            calls=[]
            def fresh(endpoint,params):
                calls.append(endpoint)
                return QueryResult(endpoint,params,True,queried_at='2026-10-11T12:00:00+08:00')
            client._from_live=fresh
            client.query_cached('search-flight')
            self.assertEqual(calls,['search-flight'])

    def test_invalid_transfer_time_is_not_a_valid_trip(self):
        item={'totalDuration':200,'ticketPrice':100,'journeys':[{'segments':[
            {'depDateTime':'2026-10-20 08:00','arrDateTime':'2026-10-20 10:00'},
            {'depDateTime':'2026-10-20 09:00','arrDateTime':'2026-10-20 11:00'}]}]}
        self.assertEqual(normalize_transport([item],'flight','outbound'),[])

    def test_wrong_route_response_is_service_error_not_no_solution(self):
        from datetime import date
        from planner import _query_leg
        from tripmodel import Member
        item={'ticketPrice':100,'totalDuration':60,'journeys':[{'segments':[{'depCityName':'广州','arrCityName':'深圳','depDateTime':'2026-10-20 08:00','arrDateTime':'2026-10-20 09:00'}]}]}
        class Client:
            def query_cached(self,endpoint,**params):
                return QueryResult(endpoint,params,True,items=[item])
        leg=_query_leg(Client(),Member('A','北京'),'flight','outbound','北京','杭州',date(2026,10,20),(3,))
        self.assertEqual(leg.options,[])
        self.assertFalse(leg.provenance[0]['ok'])
        self.assertEqual(leg.provenance[0]['failure_kind'],'parse')

    def test_cli_unconfirmed_status_is_distinct_and_is_saved(self):
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            request=Path(td)/'request.json'
            data=request_data();data.pop('hotel_quote')
            request.write_text(json.dumps(data))
            state=Path(td)/'state.json'
            result=subprocess.run([sys.executable,str(ROOT/'scripts/yiqidao.py'),'plan','--request',str(request),'--mode','fixture','--cache-dir',str(ROOT/'examples/fixtures'),'--save-state',str(state),'--format','json'],capture_output=True,text=True)
            self.assertEqual(result.returncode,5,result.stderr)
            self.assertEqual(json.loads(result.stdout)['status'],'needs_confirmation')
            self.assertEqual(json.loads(state.read_text())['outcome']['status'],'needs_confirmation')

    def test_partial_team_cost_over_budget_still_blocks(self):
        data=request_data();data.pop('hotel_quote');data['team_budget']=2000
        out=plan_trip(request_from_dict(data),FlyAIClient(mode='fixture',fixture_dir=ROOT/'examples/fixtures'))
        self.assertEqual(out.status,'no_solution')
        self.assertTrue(any('团队已知费用' in b for b in out.all_plans[0].blockers))

    def test_explicit_empty_modes_are_not_replaced_with_allowed_modes(self):
        data=request_data();data['members'][0]['modes']=[]
        req=request_from_dict(data)
        self.assertEqual(req.members[0].modes,[])
        self.assertTrue(any('交通方式' in i.message for i in blocking_issues(validate_request(req))))
