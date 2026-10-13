"""断档、查询边界和费用完整性的回归测试；不联网。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from flyai_client import FlyAIClient, QueryResult, _canonical_key, _is_cacheable
from orchestrator import plan_trip
from request_io import request_from_dict
ROOT = Path(__file__).resolve().parent.parent

class RecoveryTests(unittest.TestCase):
    def test_missing_cached_fixture_never_calls_live(self):
        with tempfile.TemporaryDirectory() as td:
            client = FlyAIClient(mode='fixture', fixture_dir=Path(td))
            client._from_live = lambda *a: self.fail('离线模式调用了真实接口')
            self.assertFalse(client.query_cached('search-flight', origin='北京').ok)

    def test_original_record_time_preserved(self):
        client = FlyAIClient(mode='fixture', fixture_dir=ROOT/'examples/fixtures')
        res = client.query_cached('search-flight', origin='北京', destination='杭州', dep_date='2026-10-20', sort_type=3)
        self.assertEqual(res.queried_at, '2026-10-10T12:00:00+08:00')
        self.assertIn('fixture:', res.source)

    def test_malformed_response_does_not_crash(self):
        client = FlyAIClient(mode='fixture', fixture_dir=ROOT/'examples/fixtures')
        for data in ([], {'status':0,'data':[]}, {'status':0,'data':{'itemList':[1]}}):
            res = client._interpret('search-flight', {}, CompletedProcess([],0,json.dumps(data),''),0)
            self.assertEqual(res.failure_kind, 'parse')

    def test_failed_queries_are_never_cached(self):
        for kind in ('auth','risk','transport','business','parse','param','budget'):
            self.assertFalse(_is_cacheable(QueryResult('search-flight',{},False,failure_kind=kind)))

    def test_stop_reason_prevents_further_subprocesses(self):
        client = FlyAIClient(binary=sys.executable, min_interval=0)
        client.stop_reason='测试停止'
        self.assertEqual(client.query('search-flight').failure_kind, 'budget')
        self.assertEqual(client.live_calls,0)

    def test_full_plan_serialization(self):
        req=request_from_dict(json.loads((ROOT/'examples/offline_replay.json').read_text()))
        client=FlyAIClient(mode='fixture',fixture_dir=ROOT/'examples/fixtures')
        out=plan_trip(req,client)
        self.assertEqual(out.status,'ok')
        serialized=json.loads(json.dumps(out.to_dict()))
        self.assertEqual(serialized['plans'][0]['team_known_cost'],3600)
        self.assertEqual(serialized['plans'][0]['common_hours'],54)
        self.assertEqual(client.live_calls,0)

    def test_service_failure_is_not_no_solution(self):
        req=request_from_dict(json.loads((ROOT/'examples/offline_replay.json').read_text()))
        with tempfile.TemporaryDirectory() as td:
            out=plan_trip(req,FlyAIClient(mode='fixture',fixture_dir=Path(td)))
            self.assertEqual(out.status,'error')

    def test_local_member_still_pays_lodging(self):
        data=json.loads((ROOT/'examples/offline_replay.json').read_text())
        data['members'][0]['origin_city']='杭州'
        out=plan_trip(request_from_dict(data),FlyAIClient(mode='fixture',fixture_dir=ROOT/'examples/fixtures'))
        self.assertEqual(out.status,'ok')
        self.assertEqual(out.plans[0].members[0].known_cost,400)
        self.assertEqual(out.plans[0].team_known_cost,2800)

    def test_partial_missing_ticket_marks_budget_unknown(self):
        import shutil
        data=json.loads((ROOT/'examples/offline_replay.json').read_text())
        with tempfile.TemporaryDirectory() as td:
            for fixture in (ROOT/'examples/fixtures').glob('*.json'):
                shutil.copy(fixture,td)
            path=Path(td)/_canonical_key('search-flight',{'origin':'北京','destination':'杭州','dep_date':'2026-10-20','sort_type':3})
            blob=json.loads(path.read_text()); blob['items'][0]['ticketPrice']=None
            path.write_text(json.dumps(blob))
            out=plan_trip(request_from_dict(data),FlyAIClient(mode='fixture',fixture_dir=Path(td)))
            member=out.plans[0].members[0]
            self.assertTrue(member.cost_incomplete)
            self.assertEqual(member.budget_state,'unknown')
            self.assertEqual(member.known_cost,800)

    def test_auth_and_rate_errors_trip_global_circuit(self):
        from unittest.mock import patch
        for stderr in ('HTTP 401 Unauthorized','HTTP 429 Too Many Requests','HTTP 451 risk control'):
            client=FlyAIClient(binary=sys.executable,min_interval=0)
            with patch('flyai_client.subprocess.run',return_value=CompletedProcess([],1,'',stderr)) as run:
                self.assertFalse(client.query('search-flight').ok)
                self.assertFalse(client.query('search-hotel').ok)
                self.assertEqual(run.call_count,1)
                self.assertEqual(client.live_calls,1)

    def test_cli_json_and_saved_state_resume(self):
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            state=str(Path(td)/'state.json')
            base=[sys.executable,str(ROOT/'scripts/yiqidao.py'),'plan','--mode','fixture','--cache-dir',str(ROOT/'examples/fixtures'),'--format','json','--save-state',state]
            first=subprocess.run(base+['--request',str(ROOT/'examples/offline_replay.json')],capture_output=True,text=True)
            self.assertEqual(first.returncode,0,first.stderr)
            self.assertEqual(json.loads(first.stdout)['plans'][0]['team_known_cost'],3600)
            resumed=subprocess.run(base+['--resume-state',state],capture_output=True,text=True)
            self.assertEqual(resumed.returncode,0,resumed.stderr)
            self.assertEqual(json.loads(resumed.stdout)['plans'],json.loads(first.stdout)['plans'])

    def test_corrupt_fixture_returns_parse_error(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/_canonical_key('search-flight',{})
            path.write_text('{oops')
            client=FlyAIClient(mode='fixture',fixture_dir=Path(td))
            self.assertEqual(client.query('search-flight').failure_kind,'parse')
