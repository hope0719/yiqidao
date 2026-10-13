"""FlyAI CLI 封装层。

职责：调用官方 flyai-cli 子命令，并把三类完全不同的结果区分开：

  1. 正常结果      -> QueryResult(ok=True, items=[...])
  2. 合法的空结果  -> QueryResult(ok=True, items=[], empty=True)
  3. 失败          -> QueryResult(ok=False, failure=...)

以下响应形状来自原项目 2026-10-10 的记录，本次本地修复未重新联网验证：

  * CLI 退出码恒为 0，即使业务失败。必须解析 JSON 判断，不能看 exit code。
  * 酒店无结果时返回 {"data": null, "message": "empty", "status": 0}
    —— status 是 0，但 data 为 null。只看 status 会误判为成功。
  * 机票查不到航线时返回 {"data": null, "message": "搜索机票低价行业失败",
    "status": 1}。该 message 无法区分"确实无航线"和"服务端异常"，
    因此原样透传，不做猜测。
  * 非法日期返回 {"status": 1, "message": "出发日期非法"}。
  * 带 --back-date 的往返一次查询会失败，必须拆成去程/返程两次单程查询。
  * 鉴权/限流失败时 stdout 为空，信息在 stderr（HTTP 401 / 429）。
  * 短时间内密集请求会触发风控，返回 HTTP 451
    "Abnormal access behavior detected by risk control"。恢复时间不确定。因此默认遇到 451 停止本轮，
    上层必须串行调用，不得并发打接口。

本模块不打印、不记录任何 API Key。凭证由 flyai-cli 自行从
~/.flyai/config.json 或 FLYAI_API_KEY 环境变量读取。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import math
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 实测可用的子命令白名单。不在表内的端点一律拒绝，避免拼错命令后
# 把 CLI 的报错误当成"查无结果"。
KNOWN_ENDPOINTS = {
    "search-flight",
    "search-train",
    "search-hotel",
    "search-poi",
    "keyword-search",
    "ai-search",
}

DEFAULT_TIMEOUT = 30

# 请求间隔只用于串行节流，不保证避免平台限流。
# 默认遇到风控停止本轮，恢复时间由服务端决定。
DEFAULT_MIN_INTERVAL = 1.2          # 两次真实请求之间的最小间隔（秒）
DEFAULT_RISK_BACKOFF = 8.0          # 命中风控后的首次等待（秒）
DEFAULT_RISK_RETRIES = 0            # 风控重试次数
DEFAULT_MAX_INTERVAL = 10.0         # 自适应节流的上限（秒）
RISK_CONTROL_MARKERS = ("451", "risk control", "abnormal access")


class FlyAIClientError(Exception):
    """客户端配置类错误（如 CLI 未安装）。"""


@dataclass
class QueryResult:
    """一次查询的完整留痕结果。"""

    endpoint: str
    params: Dict[str, Any]
    ok: bool
    items: List[Dict[str, Any]] = field(default_factory=list)
    empty: bool = False
    failure: Optional[str] = None
    failure_kind: Optional[str] = None  # transport|risk|auth|business|parse|param
    raw_message: Optional[str] = None
    system_message: Optional[str] = None
    queried_at: Optional[str] = None
    source: str = "live"  # live | fixture
    elapsed_ms: Optional[int] = None
    fixture_path: Optional[str] = None

    @property
    def provenance(self) -> Dict[str, Any]:
        """来源留痕，用于最终输出中展示查询时间与数据源。"""
        return {
            "endpoint": self.endpoint,
            "params": self.params,
            "queried_at": self.queried_at,
            "source": self.source,
            "ok": self.ok,
            "empty": self.empty,
            "failure": self.failure,
            "failure_kind": self.failure_kind,
            "raw_message": self.raw_message,
            "n_items": len(self.items),
        }


def _canonical_key(endpoint: str, params: Dict[str, Any]) -> str:
    """由端点 + 参数生成稳定的 fixture 文件名。"""
    payload = json.dumps(
        {"endpoint": endpoint, "params": params},
        sort_keys=True,
        ensure_ascii=False,
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    return f"{endpoint}__{digest}.json"


# 临时性故障：不应写入缓存，重跑可能就好了
TRANSIENT_FAILURE_KINDS = {"risk", "transport"}


def _is_cacheable(result: QueryResult) -> bool:
    """只缓存成功查询（包括合法空结果），失败均不缓存。"""
    return result.ok


def _redact(text: str) -> str:
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", str(text or ""))
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~-]+", "Bearer [REDACTED]", text)
    return text


def _brief(text: str, limit: int = 200) -> str:
    """把多行报错压成一行摘要，便于在诊断列表里阅读。"""
    flat = " ".join(_redact(text).split())
    return flat[:limit] + ("..." if len(flat) > limit else "")


def _utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class FlyAIClient:
    """调用 flyai-cli 的薄封装，支持 fixture 录制与回放。"""

    def __init__(
        self,
        binary: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        fixture_dir: Optional[Path] = None,
        mode: str = "live",
        retries: int = 1,
        retry_backoff: float = 2.0,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        risk_retries: int = DEFAULT_RISK_RETRIES,
        risk_backoff: float = DEFAULT_RISK_BACKOFF,
        max_interval: float = DEFAULT_MAX_INTERVAL,
        risk_slowdown_factor: float = 2.0,
        recover_after_successes: int = 3,
        verbose: bool = False,
        log_stream=None,
        max_calls: int = 80,
        max_seconds: float = 300,
        cache_ttl: float = 900,
    ) -> None:
        if mode not in {"live", "fixture", "record"}:
            raise ValueError(f"未知 mode: {mode}")
        self.binary = binary or os.environ.get("FLYAI_BIN") or shutil.which("flyai")
        self.binary = shutil.which(self.binary) or self.binary if self.binary else None
        if not all(math.isfinite(float(v)) for v in (timeout, max_calls, max_seconds, min_interval, cache_ttl)) or timeout <= 0 or max_calls <= 0 or max_seconds <= 0 or min_interval < 0 or cache_ttl < 0:
            raise FlyAIClientError("超时、调用上限和总时限必须为正数，请求间隔不得为负")
        self.max_calls = max_calls
        self.deadline = time.monotonic() + max_seconds
        self.cache_ttl = cache_ttl
        self.stop_reason = None
        self.timeout = timeout
        self.mode = mode
        self.retries = retries
        self.retry_backoff = retry_backoff
        self.min_interval = min_interval
        self.risk_retries = risk_retries
        self.risk_backoff = risk_backoff
        self._last_call_at = 0.0
        self._adapt_interval = min_interval
        self._success_streak = 0
        self.max_interval = max_interval
        self.risk_slowdown_factor = risk_slowdown_factor
        self.recover_after_successes = recover_after_successes
        self.risk_control_hits = 0
        self.live_calls = 0
        self.verbose = verbose
        self.log_stream = log_stream
        self.fixture_dir = Path(fixture_dir) if fixture_dir else None

        if mode in {"live", "record"}:
            if not self.binary:
                raise FlyAIClientError(
                    "未找到 flyai CLI。请先安装：npm i -g @fly-ai/flyai-cli，"
                    "或用 FLYAI_BIN 指定可执行文件路径。"
                )
            # 显式给出的路径必须真实可执行，否则会在每次查询时以
            # "CLI 异常退出" 的形式出现，难以定位。这里及早报错。
            if not Path(self.binary).is_file() or not os.access(self.binary, os.X_OK):
                raise FlyAIClientError(
                    f"flyai 可执行文件不存在：{self.binary}"
                )
        if mode in {"fixture", "record"} and not self.fixture_dir:
            raise FlyAIClientError(f"mode={mode} 需要 fixture_dir")

    # ---------- 对外主入口 ----------

    def query(self, endpoint: str, **params: Any) -> QueryResult:
        """执行一次查询。params 中值为 None 的键会被剔除。"""
        if endpoint not in KNOWN_ENDPOINTS:
            raise FlyAIClientError(
                f"未知端点 {endpoint!r}，可用：{sorted(KNOWN_ENDPOINTS)}"
            )
        clean = {
            k: v
            for k, v in params.items()
            if v is not None and not (isinstance(v, str) and not v.strip())
        }

        if self.mode == "fixture":
            return self._from_fixture(endpoint, clean)

        result = self._from_live(endpoint, clean)

        # 只有"稳定"的结果才写入缓存。风控(451)、限流(429)、超时都是
        # 临时性故障，缓存它们会让离线回放永久复现一个本可恢复的错误，
        # 也会让下次增量重算误以为该路线确实查不到。
        if self.mode == "record" and _is_cacheable(result):
            self._save_fixture(endpoint, clean, result)
        return result

    # ---------- 带缓存的调用 ----------

    def query_cached(self, endpoint: str, **params: Any) -> QueryResult:
        """命中缓存则直接返回，否则实调并写入缓存。

        用于"用户改了某个成员的条件后只重算受影响部分"：未受影响的
        (endpoint, params) 组合会命中缓存，不产生新的真实请求。
        缓存目录同时充当 fixture 目录，因此可被离线回放复用。
        """
        if self.mode == "fixture" or not self.fixture_dir:
            return self.query(endpoint, **params)

        if endpoint not in KNOWN_ENDPOINTS:
            raise FlyAIClientError(f"未知端点：{endpoint}")
        clean = {
            k: v
            for k, v in params.items()
            if v is not None and not (isinstance(v, str) and not v.strip())
        }
        path = self._fixture_path(endpoint, clean)
        if path.exists() and time.time() - path.stat().st_mtime <= self.cache_ttl:
            saved_mode = self.mode
            self.mode = "fixture"
            try:
                result = self._from_fixture(endpoint, clean)
            finally:
                self.mode = saved_mode
            result.source = f"cache:{path.name}"
            try:
                age = time.time() - datetime.fromisoformat(result.queried_at or "").timestamp()
            except (ValueError, TypeError):
                age = float("inf")
            if result.ok and 0 <= age <= self.cache_ttl:
                return result

        # 复用 query()：其中已包含"仅缓存稳定结果"的判断
        saved_mode = self.mode
        self.mode = "record"
        try:
            return self.query(endpoint, **clean)
        finally:
            self.mode = saved_mode

    # ---------- 真实调用 ----------

    def _throttle(self) -> None:
        """串行节流：保证两次真实请求之间有最小间隔，避免触发风控。

        自适应：一旦命中风控就把间隔放大（_adapt_interval），
        连续若干次成功后再缓慢收回。实测固定 1.5s 间隔仍会在
        约 10 次请求后触发 451，因此必须动态降速而非硬编码。
        """
        interval = self._adapt_interval
        if interval <= 0:
            return
        elapsed = time.monotonic() - self._last_call_at
        if elapsed < interval:
            time.sleep(interval - elapsed)

    def _log(self, msg: str) -> None:
        """把进度写到 stderr，绝不写入 stdout（stdout 只放结果）。"""
        if not self.verbose:
            return
        stream = self.log_stream if self.log_stream is not None else sys.stderr
        try:
            print(f"[flyai] {msg}", file=stream, flush=True)
        except Exception:  # noqa: BLE001 - 日志失败不得影响主流程
            pass

    def _note_risk_hit(self) -> None:
        """命中风控：显著放大请求间隔，并记录次数。"""
        self.risk_control_hits += 1
        self._adapt_interval = min(
            self._adapt_interval * self.risk_slowdown_factor, self.max_interval
        )
        self._success_streak = 0

    def _note_success(self) -> None:
        """连续成功后缓慢收回间隔，但不低于用户设定的最小值。"""
        self._success_streak += 1
        if (self._success_streak >= self.recover_after_successes
                and self._adapt_interval > self.min_interval):
            self._adapt_interval = max(
                self.min_interval,
                self._adapt_interval / self.risk_slowdown_factor,
            )
            self._success_streak = 0

    @staticmethod
    def _is_risk_control(result: QueryResult) -> bool:
        text = f"{result.raw_message or ''} {result.failure or ''}".lower()
        return any(marker in text for marker in RISK_CONTROL_MARKERS)

    def _from_live(self, endpoint: str, params: Dict[str, Any]) -> QueryResult:
        cmd = [self.binary, endpoint]
        for k, v in params.items():
            cmd.extend([f"--{k.replace('_', '-')}", str(v)])

        # 风控默认不重试；调用者显式启用时也受总时限限制
        max_attempts = max(self.retries, self.risk_retries) + 1
        last: Optional[QueryResult] = None

        for attempt in range(max_attempts):
            if self.stop_reason or self.live_calls >= self.max_calls or time.monotonic() >= self.deadline:
                self.stop_reason = self.stop_reason or "达到本轮调用次数或总时限，已保留成功缓存；请检查后续跑"
                return QueryResult(endpoint=endpoint, params=params, ok=False,
                                   failure=self.stop_reason, failure_kind="budget", source="not-called")
            self._throttle()
            if time.monotonic() >= self.deadline:
                continue
            self.live_calls += 1
            started = time.monotonic()
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=min(self.timeout, max(0.01, self.deadline - time.monotonic())),
                    check=False,
                )
            except subprocess.TimeoutExpired:
                last = QueryResult(
                    endpoint=endpoint,
                    params=params,
                    ok=False,
                    failure=f"调用超时（>{self.timeout}s）",
                    failure_kind="transport",
                    queried_at=_utc_now(),
                    source="live",
                )
                self._last_call_at = time.monotonic()
                if attempt < self.retries and time.monotonic() + self.retry_backoff < self.deadline:
                    time.sleep(self.retry_backoff)
                    continue
                return last

            elapsed = int((time.monotonic() - started) * 1000)
            self._last_call_at = time.monotonic()
            last = self._interpret(endpoint, params, proc, elapsed)
            self._log(
                f"#{self.live_calls} {endpoint} "
                f"{params.get('origin','') or params.get('dest_name','') or params.get('city_name','')}"
                f"->{params.get('destination','')} "
                f"{'ok' if last.ok else last.failure_kind} "
                f"({elapsed}ms, interval={self._adapt_interval:.1f}s)"
            )

            if last.ok:
                self._note_success()
                return last

            if last.failure_kind == "auth":
                self.stop_reason = "鉴权失败，停止本轮真实查询；修复凭证后再运行"
                return last

            # 风控 / 限流：临时性，放大全局间隔后指数退避重试
            if last.failure_kind in {"risk", "transport"} and (
                self._is_risk_control(last) or "429" in (last.raw_message or "")
            ):
                self._note_risk_hit()
                if attempt < self.risk_retries:
                    backoff = max(self.risk_backoff * (attempt + 1), self._adapt_interval)
                    self._log(
                        f"风控/限流命中（累计 {self.risk_control_hits} 次），"
                        f"退避 {backoff:.0f}s 后重试"
                    )
                    if time.monotonic() + backoff < self.deadline:
                        time.sleep(backoff)
                        continue
                self.stop_reason = "触发风控或限流，停止本轮真实查询；不要循环重试，稍后人工检查再运行"
                return last

            # 其余失败（鉴权、业务失败、解析错误）重试无意义
            return last
        return last  # type: ignore[return-value]

    def _interpret(
        self,
        endpoint: str,
        params: Dict[str, Any],
        proc: "subprocess.CompletedProcess[str]",
        elapsed_ms: int,
    ) -> QueryResult:
        base = dict(
            endpoint=endpoint,
            params=params,
            queried_at=_utc_now(),
            source="live",
            elapsed_ms=elapsed_ms,
        )
        stdout = (proc.stdout or "").strip()
        stderr = _redact((proc.stderr or "").strip())

        # CLI 退出码不可信，但 stdout 为空说明确实没拿到业务数据。
        if not stdout:
            kind, msg = self._classify_stderr(stderr, proc.returncode)
            return QueryResult(
                ok=False, failure=msg, failure_kind=kind,
                raw_message=stderr[:400], **base,
            )

        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return QueryResult(
                ok=False,
                failure=f"返回不是合法 JSON：{exc}",
                failure_kind="parse",
                raw_message=_redact(stdout[:400]),
                **base,
            )

        if not isinstance(payload, dict):
            return QueryResult(ok=False, failure="响应顶层必须是对象", failure_kind="parse", **base)
        status = payload.get("status")
        message = _redact(payload.get("message")) if payload.get("message") is not None else None
        system_message = _redact(payload.get("systemMessage")) if payload.get("systemMessage") is not None else None
        data = payload.get("data")

        # status != 0：接口层面失败（含非法日期、无航线、服务端异常）
        if status != 0:
            kind, _ = self._classify_stderr(f"{message} {system_message}", 0)
            kind = kind if kind in {"auth", "risk"} or "429" in str(message) else "business"
            return QueryResult(
                ok=False,
                failure=f"接口返回失败（status={status}）：{message}",
                failure_kind=kind,
                raw_message=message,
                system_message=system_message,
                **base,
            )

        # status == 0 但 data 为 null：实测酒店无结果时就是这样
        if data is None:
            return QueryResult(
                ok=True, items=[], empty=True, raw_message=message,
                system_message=system_message, **base,
            )

        if not isinstance(data, dict) or not isinstance(data.get("itemList", []), list):
            return QueryResult(ok=False, failure="响应 data/itemList 结构异常", failure_kind="parse", **base)
        items = data.get("itemList") or []
        if any(not isinstance(item, dict) for item in items):
            return QueryResult(ok=False, failure="itemList 必须由对象组成", failure_kind="parse", **base)
        return QueryResult(
            ok=True,
            items=items,
            empty=not items,
            raw_message=message,
            system_message=system_message,
            **base,
        )

    @staticmethod
    def _classify_stderr(stderr: str, returncode: int) -> Tuple[str, str]:
        """把 CLI 的 stderr 归类，便于上层区分'没配 key'和'服务挂了'。"""
        low = stderr.lower()
        if "401" in low or "invalid api key" in low or "unauthorized" in low:
            return "auth", "鉴权失败：API Key 无效或未配置（HTTP 401）"
        # 风控必须在 429 之前判断：实测风控返回 HTTP 451
        if any(m in low for m in RISK_CONTROL_MARKERS):
            return "risk", "触发飞猪风控（HTTP 451）：短时间内请求过于密集，本轮停止，请稍后检查"
        if "429" in low or "too many requests" in low:
            return "transport", "请求被限流（HTTP 429）"
        # 参数被后端拒绝（HTTP 400）：属于可修复的调用方错误，重试无意义。
        # 实测 flyai 的 --help 列出的参数未必被后端接受，
        # 例如 search-flight 会拒绝 totalDurationHour。
        if "400" in low or "argument not allowed" in low or "bad request" in low:
            return "param", f"接口拒绝请求参数（HTTP 400）：{_brief(stderr)}"
        if returncode != 0:
            return "transport", f"CLI 异常退出（code={returncode}）：{_brief(stderr)}"
        return "transport", "CLI 无输出"

    # ---------- fixture 录制 / 回放 ----------

    def _fixture_path(self, endpoint: str, params: Dict[str, Any]) -> Path:
        assert self.fixture_dir is not None
        return self.fixture_dir / _canonical_key(endpoint, params)

    def _save_fixture(
        self, endpoint: str, params: Dict[str, Any], result: QueryResult
    ) -> None:
        assert self.fixture_dir is not None
        self.fixture_dir.mkdir(parents=True, exist_ok=True)
        path = self._fixture_path(endpoint, params)
        blob = {
            "endpoint": endpoint,
            "params": params,
            "recorded_at": result.queried_at,
            "ok": result.ok,
            "empty": result.empty,
            "failure": result.failure,
            "failure_kind": result.failure_kind,
            "raw_message": result.raw_message,
            "system_message": result.system_message,
            "items": result.items,
        }
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
        result.fixture_path = str(path)

    def _from_fixture(self, endpoint: str, params: Dict[str, Any]) -> QueryResult:
        path = self._fixture_path(endpoint, params)
        base = dict(
            endpoint=endpoint,
            params=params,
            queried_at=_utc_now(),
            source=f"fixture:{path.name}",
        )
        if not path.exists():
            return QueryResult(
                ok=False,
                failure=f"缺少 fixture：{path.name}（请先用 --mode record 录制）",
                failure_kind="transport",
                **base,
            )
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(blob, dict) or blob.get("endpoint") != endpoint or blob.get("params") != params:
                raise ValueError("缓存端点或参数不匹配")
            if not isinstance(blob.get("items", []), list) or any(not isinstance(i, dict) for i in blob.get("items", [])):
                raise ValueError("缓存 items 结构异常")
        except (ValueError, OSError) as exc:
            return QueryResult(ok=False, failure=f"缓存损坏：{exc}", failure_kind="parse", **base)
        base["queried_at"] = blob.get("recorded_at")
        return QueryResult(
            ok=bool(blob.get("ok")),
            items=blob.get("items") or [],
            empty=bool(blob.get("empty")),
            failure=blob.get("failure"),
            failure_kind=blob.get("failure_kind"),
            raw_message=blob.get("raw_message"),
            system_message=blob.get("system_message"),
            fixture_path=str(path),
            **base,
        )
