"""独立穷举对照与旧算法反例。全部离线，不调用API。"""
import itertools,json,random,subprocess,sys,tempfile,unittest
from datetime import date,datetime,timedelta
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'scripts'))
from planner import DatePair,MemberLeg,build_city_plan
from normalize import TransportOption,Segment,parse_datetime
from tripmodel import Member,RoomingPlan,TripRequest,validate_request,blocking_issues
from request_io import request_from_dict
from solver import solve_city
from orchestrator import plan_trip
from flyai_client import QueryResult
from render import render_outcome
ROOT=Path(__file__).resolve().parent.parent
PAIR=DatePair(date(2026,10,20),date(2026,10,22),2)

def option(direction,price,duration,anchor):
    if direction=='outbound':
        arrival=datetime(2026,10,20,anchor); departure=arrival-timedelta(minutes=duration)
    else:
        departure=datetime(2026,10,22,anchor); arrival=departure+timedelta(minutes=duration)
    segment=Segment('TEST',None,None,'北京' if direction=='outbound' else '杭州',None,departure,'杭州' if direction=='outbound' else '北京',None,arrival,duration,None)
    return TransportOption('flight',direction,price,price is None,duration,'直达',[segment])

def legs(outbound,ret):
    return {'outbound':MemberLeg('A','outbound',options=outbound,feasible=outbound,provenance=[{'ok':True}]),'return':MemberLeg('A','return',options=ret,feasible=ret,provenance=[{'ok':True}])}

def req(members):
    return TripRequest(members,['杭州'],1000,RoomingPlan(2,2),2,24,date(2026,10,11),arrival_buffer_minutes=0,departure_buffer_minutes=0)

class SearchLogicTests(unittest.TestCase):
    def test_mixed_roundtrip_can_succeed_when_all_three_greedy_strategies_fail(self):
        a=Member('A','北京',budget=280,max_roundtrip_minutes=300)
        b=Member('B','上海',budget=400,max_roundtrip_minutes=300)
        request=req([a,b])
        data={'A':legs([option('outbound',100,200,11),option('outbound',160,60,8)],[option('return',100,200,14),option('return',160,60,18)]),
              'B':legs([option('outbound',100,60,11)],[option('return',100,60,18)])}
        for strategy in ('cheapest','max_common','min_burden'):
            self.assertFalse(build_city_plan(None,request,'杭州',PAIR,data,strategy,None).feasible)
        solved=solve_city(None,request,'杭州',PAIR,data,None)
        self.assertTrue(solved.feasible)
        self.assertEqual(solved.team_known_cost,460)
        self.assertEqual(solved.members[0].total_minutes,260)

    def test_solver_matches_independent_brute_force_for_40_small_cases(self):
        rng=random.Random(42)
        for case in range(40):
            members=[Member(str(i),'北京',budget=rng.randint(200,450),max_roundtrip_minutes=rng.randint(120,320)) for i in range(3)]
            request=req(members);request.team_budget=rng.randint(600,1200);request.min_common_hours=rng.randint(20,60)
            data={m.name:legs([option('outbound',rng.randint(50,200),rng.randint(40,180),rng.randint(8,20)) for _ in range(2)],[option('return',rng.randint(50,200),rng.randint(40,180),rng.randint(8,20)) for _ in range(2)]) for m in members}
            brute=[]
            pairs=[list(itertools.product(data[m.name]['outbound'].feasible,data[m.name]['return'].feasible)) for m in members]
            for combination in itertools.product(*pairs):
                costs=[ob.price+rt.price for ob,rt in combination]
                if any(cost>m.budget or ob.total_duration_min+rt.total_duration_min>m.max_roundtrip_minutes for m,cost,(ob,rt) in zip(members,costs,combination)):
                    continue
                hours=(min(rt.dep_time for _,rt in combination)-max(ob.arr_time for ob,_ in combination)).total_seconds()/3600
                if hours>=request.min_common_hours and sum(costs)<=request.team_budget:
                    brute.append(sum(costs))
            solved=solve_city(None,request,'杭州',PAIR,data,None)
            self.assertEqual(solved.feasible,bool(brute),f'case {case}')
            if brute:self.assertEqual(solved.team_known_cost,min(brute),f'case {case}')

    def test_second_date_is_searched_after_first_has_no_transport(self):
        data=json.loads((ROOT/'examples/hotel_unconfirmed.json').read_text())
        for m in data['members']:
            m.update(dep_date_start='2026-10-20',dep_date_end='2026-10-21',ret_date_start='2026-10-22',ret_date_end='2026-10-23')
        class Client:
            def query_cached(self,endpoint,**params):
                if endpoint=='search-flight':
                    day=params['dep_date']
                    if day in ('2026-10-20','2026-10-22'):return QueryResult(endpoint,params,True,items=[],empty=True)
                    ret=params['origin']=='杭州'
                    items=[{'ticketPrice':100,'totalDuration':120,'journeys':[{'segments':[{'depCityName':params['origin'],'arrCityName':params['destination'],'depDateTime':day+(' 16:00' if ret else ' 08:00'),'arrDateTime':day+(' 18:00' if ret else ' 10:00')}]}]}]
                elif endpoint=='search-hotel':items=[{'name':'示例酒店','price':'¥10'}]
                else:items=[]
                return QueryResult(endpoint,params,True,items=items)
        out=plan_trip(request_from_dict(data),Client())
        self.assertEqual(out.status,'needs_confirmation')
        self.assertEqual(out.plans[0].date_pair.dep_date,date(2026,10,21))

    def test_truncated_date_search_cannot_claim_no_solution(self):
        data=json.loads((ROOT/'examples/hotel_unconfirmed.json').read_text())
        for m in data['members']:
            m.update(dep_date_end='2026-10-23',ret_date_end='2026-10-25')
        class EmptyClient:
            def query_cached(self,endpoint,**params):return QueryResult(endpoint,params,True,items=[],empty=True)
        out=plan_trip(request_from_dict(data),EmptyClient(),max_date_pairs=1)
        self.assertEqual(out.status,'search_incomplete')
        self.assertIn('不能判定无解',render_outcome(out,request_from_dict(data)))

    def test_specific_date_alias_is_exact_not_latest_possible_day(self):
        data=json.loads((ROOT/'examples/hotel_unconfirmed.json').read_text())
        for m in data['members']:
            for field in ('dep_date_start','dep_date_end','ret_date_start','ret_date_end'):m.pop(field)
            m.update(dep_date='2026-10-20',ret_date='2026-10-22')
        request=request_from_dict(data)
        self.assertEqual(request.members[0].dep_date_start,request.members[0].dep_date_end)
        self.assertEqual(request.members[0].ret_date_start,request.members[0].ret_date_end)

    def test_missing_dates_are_asked_before_cli_initialization(self):
        data=json.loads((ROOT/'examples/hotel_unconfirmed.json').read_text())
        for m in data['members']:
            for field in ('dep_date_start','dep_date_end','ret_date_start','ret_date_end'):m.pop(field)
        with tempfile.TemporaryDirectory() as td:
            result=subprocess.run([sys.executable,str(ROOT/'scripts/yiqidao.py'),'plan','--request-json',json.dumps(data),'--flyai-bin','/nonexistent/flyai','--format','json','--save-state',str(Path(td)/'state.json')],capture_output=True,text=True)
            self.assertEqual(result.returncode,2,result.stderr)
            self.assertTrue(json.loads(result.stdout)['questions'])

    def test_synthetic_report_never_claims_live_data(self):
        from flyai_client import FlyAIClient
        request=request_from_dict(json.loads((ROOT/'examples/offline_replay.json').read_text()))
        text=render_outcome(plan_trip(request,FlyAIClient(mode='fixture',fixture_dir=ROOT/'examples/fixtures')),request)
        self.assertNotIn('数据来源：飞猪 FlyAI 实时查询',text)
        self.assertIn('数据来源：虚构测试数据回放',text)

    def test_home_deadline_is_not_just_station_arrival(self):
        from planner import _filter_leg
        back=option('return',100,60,18)
        member=Member('A','北京',back_home_by=datetime(2026,10,22,19,30))
        leg=MemberLeg('A','return',options=[back])
        _filter_leg(leg,member,PAIR,home_buffer_minutes=60)
        self.assertEqual(leg.feasible,[])
        self.assertIn('到站后预留60分钟',leg.reject_reasons[0])

    def test_date_only_transport_time_does_not_become_midnight(self):
        self.assertIsNone(parse_datetime('2026-10-20'))

    def test_api_underreported_duration_cannot_bypass_duration_cap(self):
        from normalize import normalize_transport
        item={'ticketPrice':100,'totalDuration':10,'journeys':[{'segments':[{'depDateTime':'2026-10-20 08:00','arrDateTime':'2026-10-20 10:00'}]}]}
        self.assertEqual(normalize_transport([item],'flight','outbound')[0].total_duration_min,120)

    def test_missing_room_count_is_not_assumed_one_room(self):
        data=json.loads((ROOT/'examples/hotel_unconfirmed.json').read_text())
        data['rooming'].pop('rooms')
        issues=blocking_issues(validate_request(request_from_dict(data)))
        self.assertTrue(any('房间数' in issue.message for issue in issues))

    def test_zero_rooming_nights_are_not_replaced_with_one(self):
        data=json.loads((ROOT/'examples/hotel_unconfirmed.json').read_text())
        data['rooming']['nights']=0
        self.assertEqual(request_from_dict(data).rooming.nights,0)
        self.assertTrue(blocking_issues(validate_request(request_from_dict(data))))

    def test_partial_old_date_window_remains_usable(self):
        data=json.loads((ROOT/'examples/hotel_unconfirmed.json').read_text())
        for m in data['members']:m['dep_date_start']='2026-10-01'
        issues=blocking_issues(validate_request(request_from_dict(data),date(2026,10,11)))
        self.assertFalse(issues)

    def test_overlapping_return_window_can_start_before_departure_window(self):
        data=json.loads((ROOT/'examples/hotel_unconfirmed.json').read_text())
        for m in data['members']:m['ret_date_start']='2026-10-19'
        self.assertFalse(blocking_issues(validate_request(request_from_dict(data))))

    def test_error_response_redacts_key_and_json_auth_trips_circuit(self):
        from flyai_client import FlyAIClient
        from unittest.mock import patch
        from subprocess import CompletedProcess
        secret='sk-'+'X'*32
        client=FlyAIClient(binary=sys.executable,min_interval=0)
        payload=json.dumps({'status':1,'message':'401 Unauthorized '+secret})
        with patch('flyai_client.subprocess.run',return_value=CompletedProcess([],0,payload,'')) as run:
            res=client.query('search-flight')
            self.assertEqual(res.failure_kind,'auth')
            self.assertNotIn(secret,str(res.provenance))
            client.query('search-hotel')
            self.assertEqual(run.call_count,1)

    def test_unknown_ticket_price_is_not_ranked_as_a_cheaper_city(self):
        from planner import CityPlan,MemberPlan,rank_plans
        unknown=CityPlan('甲',PAIR,team_known_cost=100,team_cost_incomplete=True,feasible=True,members=[MemberPlan('A','北京',transport_missing=True)])
        known=CityPlan('乙',PAIR,team_known_cost=500,team_cost_incomplete=True,feasible=True,members=[MemberPlan('A','北京',transport_missing=False)])
        self.assertEqual(rank_plans([unknown,known],req([]))[0].city,'乙')
