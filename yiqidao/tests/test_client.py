"""FlyAI 客户端与端到端回放测试。不联网。

关键：这里用**假的 flyai 可执行文件**驱动真实代码路径，
以覆盖实测发现的接口陷阱。响应体形状参照 2026-10-10 真实返回，
数值为编造的测试数据。
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from flyai_client import FlyAIClient, FlyAIClientError, QueryResult  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "examples" / "fixtures"


def make_fake_binary(script_body: str) -> str:
    """生成一个假的 flyai 可执行脚本，用于模拟各种接口返回。"""
    fd, path = tempfile.mkstemp(suffix="_fake_flyai")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\n" + script_body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class TestResultClassification(unittest.TestCase):
    """实测三类返回必须被正确区分。"""

    def _client(self, stdout: str, stderr: str = "", code: int = 0) -> FlyAIClient:
        body = f'''
printf '%s' '{stdout}'
printf '%s' '{stderr}' >&2
exit {code}
'''
        binary = make_fake_binary(body)
        self.addCleanup(os.unlink, binary)
        return FlyAIClient(binary=binary, timeout=30, min_interval=0)

    def test_normal_result(self):
        payload = json.dumps({
            "data": {"itemList": [{"ticketPrice": "750.00", "journeys": []}]},
            "message": "success", "status": 0, "systemMessage": None,
        })
        c = self._client(payload)
        res = c.query("search-flight", origin="上海", destination="三亚",
                      dep_date="2026-10-20", sort_type=3)
        self.assertTrue(res.ok)
        self.assertFalse(res.empty)
        self.assertEqual(len(res.items), 1)

    def test_hotel_empty_with_status_zero(self):
        """实测陷阱：酒店无结果时 status=0 但 data=null。只看 status 会误判。"""
        payload = json.dumps({
            "data": None, "message": "empty", "status": 0, "systemMessage": None,
        })
        c = self._client(payload)
        res = c.query("search-hotel", dest_name="杭州")
        self.assertTrue(res.ok, "空结果是成功的一种，不是失败")
        self.assertTrue(res.empty)
        self.assertEqual(res.items, [])

    def test_no_route_status_one(self):
        """实测：无航线返回 status=1。"""
        payload = json.dumps({
            "data": None, "message": "搜索机票低价行业失败",
            "status": 1, "systemMessage": None,
        })
        c = self._client(payload)
        res = c.query("search-flight", origin="拉萨", destination="三亚",
                      dep_date="2026-10-20", sort_type=3)
        self.assertFalse(res.ok)
        self.assertEqual(res.failure_kind, "business")
        self.assertIn("搜索机票低价行业失败", res.failure)

    def test_invalid_date(self):
        """实测：过去日期返回 '出发日期非法'。"""
        payload = json.dumps({
            "data": None, "message": "出发日期非法", "status": 1, "systemMessage": None,
        })
        c = self._client(payload)
        res = c.query("search-flight", origin="上海", destination="三亚",
                      dep_date="2020-01-01", sort_type=3)
        self.assertFalse(res.ok)
        self.assertIn("出发日期非法", res.failure)

    def test_auth_failure(self):
        """实测：假 key -> HTTP 401，stdout 为空。"""
        c = self._client("", stderr="MCP HTTP 401: Unauthorized\\n"
                                    'Body: {"error":{"message":"Invalid API key"}}', code=1)
        res = c.query("search-flight", origin="上海", destination="三亚",
                      dep_date="2026-10-20", sort_type=3)
        self.assertFalse(res.ok)
        self.assertEqual(res.failure_kind, "auth")

    def test_risk_control_451(self):
        """实测：密集请求 -> HTTP 451 风控。必须被单独归类，不与 429 混淆。"""
        c = self._client("", stderr="MCP HTTP 451: Unknown\\n"
                                    'Body: {"error":{"message":"Abnormal access '
                                    'behavior detected by risk control"}}', code=1)
        c.risk_retries = 0
        c.risk_backoff = 0
        res = c.query("search-flight", origin="广州", destination="厦门",
                      dep_date="2026-10-20", sort_type=3)
        self.assertFalse(res.ok)
        self.assertEqual(res.failure_kind, "risk", f"实际={res.failure_kind}")
        self.assertIn("451", res.failure)

    def test_rate_limit_429(self):
        """实测：试用额度耗尽 -> HTTP 429。"""
        c = self._client("", stderr="MCP HTTP 429: Too Many Requests\\n"
                                    'Body: {"error":{"message":"Trial limit reached"}}', code=1)
        c.retries = 0
        c.risk_retries = 0
        res = c.query("search-flight", origin="上海", destination="三亚",
                      dep_date="2026-10-20", sort_type=3)
        self.assertFalse(res.ok)
        self.assertEqual(res.failure_kind, "transport")

    def test_invalid_json(self):
        c = self._client("not json at all")
        res = c.query("search-flight", origin="上海", destination="三亚",
                      dep_date="2026-10-20", sort_type=3)
        self.assertFalse(res.ok)
        self.assertEqual(res.failure_kind, "parse")

    def test_timeout(self):
        binary = make_fake_binary("sleep 5\n")
        self.addCleanup(os.unlink, binary)
        c = FlyAIClient(binary=binary, timeout=1, min_interval=0, retries=0)
        res = c.query("search-flight", origin="上海", destination="三亚",
                      dep_date="2026-10-20", sort_type=3)
        self.assertFalse(res.ok)
        self.assertIn("超时", res.failure)

    def test_unknown_endpoint_rejected(self):
        """拼错的端点必须立即报错，不能被当成'查无结果'。"""
        binary = make_fake_binary("echo '{}'")
        self.addCleanup(os.unlink, binary)
        c = FlyAIClient(binary=binary, min_interval=0)
        with self.assertRaises(FlyAIClientError):
            c.query("search-flights-typo", origin="上海")

    def test_missing_binary(self):
        with self.assertRaises(FlyAIClientError):
            FlyAIClient(binary="/nonexistent/flyai", mode="live", min_interval=0)


class TestThrottle(unittest.TestCase):
    def test_min_interval_enforced(self):
        """节流：两次真实请求之间必须满足最小间隔。"""
        import time as _time
        body = 'echo \'{"data":{"itemList":[]},"status":0,"message":"success"}\''
        binary = make_fake_binary(body)
        self.addCleanup(os.unlink, binary)
        c = FlyAIClient(binary=binary, min_interval=0.6, timeout=30)
        start = _time.monotonic()
        for _ in range(3):
            c.query("search-flight", origin="上海", destination="杭州",
                    dep_date="2026-10-20", sort_type=3)
        elapsed = _time.monotonic() - start
        # 3 次调用之间至少有 2 个间隔
        self.assertGreaterEqual(elapsed, 1.1, f"实际耗时 {elapsed:.2f}s，节流未生效")


class TestFixtureRoundTrip(unittest.TestCase):
    def test_record_then_replay(self):
        """record 录制的响应可被 fixture 模式离线回放，结果一致。"""
        payload = json.dumps({
            "data": {"itemList": [{
                "ticketPrice": "750.00",
                "totalDuration": "185",
                "jumpUrl": "https://example.test/b",
                "journeys": [{"journeyType": "直达", "totalDuration": "185",
                              "segments": [{
                                  "marketingTransportNo": "FM9537",
                                  "depDateTime": "2026-10-20 06:55:00",
                                  "arrDateTime": "2026-10-20 10:00:00",
                                  "depStationName": "虹桥", "arrStationName": "凤凰",
                                  "duration": "185",
                              }]}],
            }]},
            "message": "success", "status": 0,
        })
        binary = make_fake_binary(f"printf '%s' '{payload}'")
        self.addCleanup(os.unlink, binary)

        with tempfile.TemporaryDirectory() as td:
            rec = FlyAIClient(binary=binary, fixture_dir=Path(td),
                              mode="record", min_interval=0)
            params = dict(origin="上海", destination="三亚",
                          dep_date="2026-10-20", sort_type=3)
            live = rec.query("search-flight", **params)
            self.assertTrue(live.ok)
            self.assertEqual(live.source, "live")
            self.assertEqual(len(list(Path(td).glob("*.json"))), 1)

            play = FlyAIClient(fixture_dir=Path(td), mode="fixture")
            replay = play.query("search-flight", **params)
            self.assertTrue(replay.ok)
            self.assertEqual(replay.items, live.items)
            self.assertTrue(replay.source.startswith("fixture:"))

    def test_missing_fixture_is_error(self):
        with tempfile.TemporaryDirectory() as td:
            c = FlyAIClient(fixture_dir=Path(td), mode="fixture")
            res = c.query("search-flight", origin="上海", destination="三亚",
                          dep_date="2026-10-20", sort_type=3)
            self.assertFalse(res.ok)
            self.assertIn("缺少 fixture", res.failure)

    def test_cached_query_avoids_second_call(self):
        """query_cached 命中缓存后不再发起真实请求（支撑增量重算）。"""
        counter = tempfile.NamedTemporaryFile(mode="w", suffix=".count", delete=False)
        counter.write("0")
        counter.close()
        self.addCleanup(os.unlink, counter.name)

        payload = json.dumps({"data": {"itemList": []}, "status": 0, "message": "success"})
        body = f'''
n=$(cat {counter.name})
echo $((n+1)) > {counter.name}
printf '%s' '{payload}'
'''
        binary = make_fake_binary(body)
        self.addCleanup(os.unlink, binary)

        with tempfile.TemporaryDirectory() as td:
            c = FlyAIClient(binary=binary, fixture_dir=Path(td),
                            mode="live", min_interval=0)
            params = dict(origin="上海", destination="杭州",
                          dep_date="2026-10-20", sort_type=3)
            r1 = c.query_cached("search-flight", **params)
            r2 = c.query_cached("search-flight", **params)
            r3 = c.query_cached("search-flight", **params)
            self.assertEqual(c.live_calls, 1, "只应发起一次真实请求")
            self.assertTrue(r2.source.startswith("cache:"))
            self.assertTrue(r3.source.startswith("cache:"))
            with open(counter.name) as f:
                self.assertEqual(f.read().strip(), "1")


class TestEndToEndFixtureReplay(unittest.TestCase):
    """用 examples/fixtures 里标注好的测试数据做端到端离线回放。"""

    def test_replay_from_committed_fixtures(self):
        if not FIXTURE_DIR.exists() or not list(FIXTURE_DIR.glob("*.json")):
            self.skipTest(f"无 fixture：{FIXTURE_DIR}")
        from orchestrator import plan_trip
        from request_io import request_from_json

        req_file = Path(__file__).resolve().parent.parent / "examples" / "offline_replay.json"
        if not req_file.exists():
            self.skipTest(f"无示例请求：{req_file}")

        req = request_from_json(req_file.read_text(encoding="utf-8"))
        client = FlyAIClient(fixture_dir=FIXTURE_DIR, mode="fixture")
        outcome = plan_trip(req, client, today=req.today or date(2026, 10, 10))
        # 离线回放必须能跑完，且所有来源都标注为 fixture（不得伪装成实时）
        self.assertIn(outcome.status, {"ok", "no_solution", "needs_input"})
        for prov in outcome.provenance:
            self.assertTrue(
                str(prov.get("source", "")).startswith(("fixture", "cache")),
                f"离线回放出现非 fixture 来源：{prov}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
