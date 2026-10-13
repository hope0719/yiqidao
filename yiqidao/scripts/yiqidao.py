#!/usr/bin/env python3
"""一起到 — 多人异地会合旅行助手 · CLI 入口。

用法：
  # 真实调用飞猪（需已配置 flyai CLI 与 API Key）
  python3 yiqidao.py plan --request examples/hangzhou_3cities.json

  # 录制：真实调用并把响应存入缓存目录，供离线回放
  python3 yiqidao.py plan --request req.json --mode record --cache-dir .cache

  # 回放：完全不联网，只用已录制的响应（结果标注 source=fixture）
  python3 yiqidao.py plan --request req.json --mode fixture --cache-dir .cache

  # 只校验请求，不发起查询
  python3 yiqidao.py validate --request req.json

退出码：0=已知交通住宿可比较 2=需补充信息 3=无可行方案 4=运行错误 5=方案待确认 6=搜索未完成

安全：本脚本不读取、不打印、不写入任何 API Key。凭证由 flyai-cli 自行管理。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Dict, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from flyai_client import FlyAIClient, FlyAIClientError  # noqa: E402
from orchestrator import PlanOutcome, plan_trip  # noqa: E402
from render import render_outcome  # noqa: E402
from request_io import request_from_dict, request_from_json, request_to_dict  # noqa: E402
from tripmodel import TripRequest  # noqa: E402

EXIT_OK = 0
EXIT_NEEDS_INPUT = 2
EXIT_NO_SOLUTION = 3
EXIT_ERROR = 4
EXIT_NEEDS_CONFIRMATION = 5
EXIT_SEARCH_INCOMPLETE = 6


def _load_request(path: Optional[str], inline: Optional[str]) -> TripRequest:
    if inline:
        return request_from_dict(json.loads(inline))
    if path:
        p = Path(path)
        if not p.exists():
            raise ValueError(f"请求文件不存在：{p}")
        return request_from_json(p.read_text(encoding="utf-8"))
    raise ValueError("必须提供 --request <file> 或 --request-json <json>")


def _atomic_json(path: Path, blob: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _save_state(req: TripRequest, outcome: PlanOutcome, path: Optional[str]) -> None:
    """把请求与结论落盘，供"修改某人条件后增量重算"使用。"""
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    blob = {
        "request": request_to_dict(req),
        "outcome": outcome.to_dict(),
        "plans": outcome.to_dict()["plans"],
    }
    _atomic_json(p, blob)


def cmd_plan(args: argparse.Namespace) -> int:
    if args.resume_state:
        if args.request or args.request_json:
            raise ValueError("--resume-state 与 --request/--request-json 只能选择一种")
        state = json.loads(Path(args.resume_state).read_text(encoding="utf-8"))
        req = request_from_dict(state["request"])
    else:
        req = _load_request(args.request, args.request_json)
    if args.max_date_pairs <= 0 or args.max_plans <= 0:
        raise ValueError("max-plans 必须为正数")

    # today 显式注入，保证结果可复现（不隐式依赖运行时系统日期）
    if args.today:
        req.today = date.fromisoformat(args.today)
    elif args.mode != "fixture" or req.today is None:
        req.today = datetime.now(ZoneInfo("Asia/Shanghai")).date()

    cache_dir = Path(args.cache_dir) if args.cache_dir else None

    from tripmodel import blocking_issues, validate_request
    checkpoint = Path(args.save_state)
    _atomic_json(checkpoint, {"request": request_to_dict(req), "status": "running"})
    if blocking_issues(validate_request(req, req.today)) or not req.candidates:
        outcome = plan_trip(req, None, today=req.today)
    else:
        try:
            client = FlyAIClient(
                binary=args.flyai_bin, timeout=args.timeout, fixture_dir=cache_dir,
                mode=args.mode, max_calls=args.max_calls, max_seconds=args.max_seconds,
                cache_ttl=args.cache_ttl, verbose=args.verbose,
                **({"min_interval": args.min_interval} if args.min_interval is not None else {}),
            )
            outcome = plan_trip(req, client, today=req.today, max_plans=args.max_plans,
                                cached=not args.no_cache, time_optimal=args.time_optimal,
                                max_date_pairs=args.max_date_pairs)
        except (FlyAIClientError, OSError) as exc:
            outcome = PlanOutcome(status="error", error=str(exc))
    # 完整结果先落盘，输出管道关闭或写Markdown失败也不会丢失方案。
    _save_state(req, outcome, args.save_state)

    text = (json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2)
            if args.format == "json" else render_outcome(outcome, req))
    print(text)
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text + "\n", encoding="utf-8")

    return {
        "ok": EXIT_OK,
        "needs_input": EXIT_NEEDS_INPUT,
        "no_solution": EXIT_NO_SOLUTION,
        "error": EXIT_ERROR,
        "needs_confirmation": EXIT_NEEDS_CONFIRMATION,
        "search_incomplete": EXIT_SEARCH_INCOMPLETE,
    }.get(outcome.status, EXIT_ERROR)


def cmd_validate(args: argparse.Namespace) -> int:
    from tripmodel import blocking_issues, questions_for_user, validate_request

    req = _load_request(args.request, args.request_json)
    if args.today:
        req.today = date.fromisoformat(args.today)
    issues = validate_request(req, (date.fromisoformat(args.today) if args.today else (req.today if args.mode == "fixture" and req.today else datetime.now(ZoneInfo("Asia/Shanghai")).date())))
    payload = {
        "ok": not blocking_issues(issues),
        "n_issues": len(issues),
        "n_blocking": len(blocking_issues(issues)),
        "questions": questions_for_user(issues),
        "issues": [i.to_dict() for i in issues],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK if payload["ok"] else EXIT_NEEDS_INPUT


def cmd_probe(args: argparse.Namespace) -> int:
    """能力探测：打印某个端点的原始返回摘要，用于验证接口可用性。"""
    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    try:
        client = FlyAIClient(
            binary=args.flyai_bin, timeout=args.timeout,
            fixture_dir=cache_dir, mode=args.mode,
            max_calls=args.max_calls, max_seconds=args.max_seconds, cache_ttl=args.cache_ttl,
        )
    except FlyAIClientError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    params = json.loads(args.params) if args.params else {}
    res = client.query(args.endpoint, **params)
    print(json.dumps({
        "ok": res.ok,
        "empty": res.empty,
        "failure": res.failure,
        "failure_kind": res.failure_kind,
        "raw_message": res.raw_message,
        "n_items": len(res.items),
        "queried_at": res.queried_at,
        "source": res.source,
        "first_item_keys": sorted(res.items[0].keys()) if res.items else [],
    }, ensure_ascii=False, indent=2))
    return EXIT_OK if res.ok else EXIT_ERROR


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="yiqidao",
        description="一起到 — 多人异地会合旅行助手（基于飞猪 FlyAI）",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--mode", choices=("live", "fixture", "record"), default="live",
                       help="live=真实调用；record=真实调用并缓存；fixture=离线回放")
        p.add_argument("--cache-dir", default=".cache", help="缓存/fixture 目录（默认 .cache）")
        p.add_argument("--flyai-bin", default=None, help="flyai 可执行文件路径")
        p.add_argument("--timeout", type=int, default=30, help="单次查询超时秒数")
        p.add_argument("--max-calls", type=int, default=80, help="本轮最多真实调用次数")
        p.add_argument("--max-seconds", type=float, default=300, help="本轮真实查询总时限")
        p.add_argument("--cache-ttl", type=float, default=900, help="实时缓存有效秒数")
        p.add_argument("--debug", action="store_true", help="出错时打印堆栈")
        p.add_argument("-v", "--verbose", action="store_true",
                       help="把查询进度写到 stderr（长任务监控用）")

    p_plan = sub.add_parser("plan", help="规划会合方案")
    p_plan.add_argument("--resume-state", help="从保存的状态读取请求，复用仍有效的缓存继续")
    p_plan.add_argument("--output", help="结果文件路径")
    p_plan.add_argument("--request", help="请求 JSON 文件路径")
    p_plan.add_argument("--request-json", help="内联请求 JSON 字符串")
    p_plan.add_argument("--today", help="固定'今天'（YYYY-MM-DD），便于复现")
    p_plan.add_argument("--format", choices=("markdown", "json"), default="markdown")
    p_plan.add_argument("--max-date-pairs", type=int, default=3, help="最多查询几组可行日期（默认3）")
    p_plan.add_argument("--max-plans", type=int, default=3, help="最多输出几个方案")
    p_plan.add_argument("--no-cache", action="store_true", help="禁用缓存复用")
    p_plan.add_argument("--time-optimal", action="store_true",
                        help="追加时刻排序查询以更精确最大化共同停留时间（查询量翻倍，风控风险上升）")
    p_plan.add_argument("--min-interval", type=float, default=None,
                        help="两次真实请求的最小间隔秒数（默认 1.2，用于串行节流）")
    p_plan.add_argument("--save-state", default="outputs/latest-state.json", help="把请求与结论写入该文件，供增量重算")
    common(p_plan)
    p_plan.set_defaults(func=cmd_plan)

    p_val = sub.add_parser("validate", help="只校验请求，不发起查询")
    p_val.add_argument("--request")
    p_val.add_argument("--request-json")
    p_val.add_argument("--today")
    common(p_val)
    p_val.set_defaults(func=cmd_validate)

    p_probe = sub.add_parser("probe", help="探测单个 FlyAI 端点")
    p_probe.add_argument("--endpoint", required=True)
    p_probe.add_argument("--params", help="JSON 形式的参数")
    common(p_probe)
    p_probe.set_defaults(func=cmd_probe)

    return ap


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError, OSError, FlyAIClientError) as exc:
        print(f"输入或运行错误：{exc}", file=sys.stderr)
        if args.debug:
            raise
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
