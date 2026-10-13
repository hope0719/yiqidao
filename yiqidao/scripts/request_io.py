"""请求的 JSON 序列化 / 反序列化。

Skill 的实际使用方式是：模型把自然语言抽取成结构化 JSON，交给脚本计算。
抽取由模型完成，但所有校验、比较、加总、排序都在脚本里做，模型不得心算。

字段命名刻意区分三组易混概念：
  * budget（每人）/ team_budget（团队总额）
  * max_oneway_minutes（单程）/ max_roundtrip_minutes（往返合计）
  * dep_date_*（可出发窗）/ back_home_by（必须到家的硬截止）
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from tripmodel import MODE_FLIGHT, MODE_TRAIN, Member, RoomingPlan, TripRequest

_MODE_ALIASES = {
    "flight": MODE_FLIGHT, "plane": MODE_FLIGHT, "air": MODE_FLIGHT,
    "飞机": MODE_FLIGHT, "机票": MODE_FLIGHT, "航班": MODE_FLIGHT,
    "train": MODE_TRAIN, "rail": MODE_TRAIN,
    "火车": MODE_TRAIN, "高铁": MODE_TRAIN, "动车": MODE_TRAIN,
}


def _parse_date(v: Any) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v).strip())


def _parse_datetime(v: Any) -> Optional[datetime]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    text = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间：{v!r}")


def _parse_hours_to_minutes(v: Any) -> Optional[int]:
    """接受分钟整数或小时浮点（如 4.5）。调用方需说明单位。"""
    if v is None or v == "":
        return None
    return int(round(float(v)))


def parse_modes(raw: Any) -> List[str]:
    """解析交通方式。未识别的值原样保留，交由校验层报错。

    刻意不在此处静默回退成"飞机+火车"——那会吞掉用户的真实约束
    （例如用户只接受"自驾"），违反"不静默忽略用户约束"。
    仅当用户完全未指定时才默认两种方式都接受。
    """
    if raw is None:
        return [MODE_FLIGHT, MODE_TRAIN]
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("modes 必须是字符串或数组")
    if not raw:
        return []
    out: List[str] = []
    for item in raw:
        stripped = str(item).strip()
        mapped = _MODE_ALIASES.get(stripped.lower()) or _MODE_ALIASES.get(stripped)
        token = mapped or stripped
        if token and token not in out:
            out.append(token)
    return out


def member_from_dict(d: Dict[str, Any], idx: int) -> Member:
    name = d.get("name") or f"成员{idx + 1}"
    oneway = d.get("max_oneway_minutes")
    if oneway is None and d.get("max_oneway_hours") is not None:
        oneway = _parse_hours_to_minutes(float(d["max_oneway_hours"]) * 60)
    rt = d.get("max_roundtrip_minutes")
    if rt is None and d.get("max_roundtrip_hours") is not None:
        rt = _parse_hours_to_minutes(float(d["max_roundtrip_hours"]) * 60)

    return Member(
        name=str(name),
        origin_city=str(d.get("origin_city") or d.get("from") or ""),
        dep_date_start=_parse_date(d.get("dep_date_start") or d.get("dep_date")),
        dep_date_end=_parse_date(d.get("dep_date_end") or d.get("dep_date")),
        ret_date_start=_parse_date(d.get("ret_date_start") or d.get("ret_date")),
        ret_date_end=_parse_date(d.get("ret_date_end") or d.get("ret_date")),
        back_home_by=_parse_datetime(d.get("back_home_by")),
        budget=float(d["budget"]) if d.get("budget") not in (None, "") else None,
        max_oneway_minutes=_parse_hours_to_minutes(oneway) if oneway is not None else None,
        max_roundtrip_minutes=_parse_hours_to_minutes(rt) if rt is not None else None,
        modes=parse_modes(d.get("modes")),
        notes=d.get("notes"),
    )


def rooming_from_dict(d: Optional[Dict[str, Any]], nights: Optional[int]) -> Optional[RoomingPlan]:
    if not d:
        return None
    n = d.get("nights") if d.get("nights") is not None else nights
    return RoomingPlan(
        rooms=int(d.get("rooms", 0)),
        nights=int(n if n is not None else 0),
        assignments=d.get("assignments"),
        note=d.get("note"),
    )


def request_from_dict(data: Dict[str, Any]) -> TripRequest:
    if not isinstance(data, dict):
        raise ValueError("请求必须是 JSON 对象")
    if not isinstance(data.get("candidates", []), list):
        raise ValueError("candidates 必须是城市数组")
    raw_members = data.get("members") or []
    if not isinstance(raw_members, list):
        raise ValueError("members 必须是数组")
    if any(not isinstance(m, dict) for m in raw_members):
        raise ValueError("members 每一项必须是对象")
    if data.get("rooming") is not None and not isinstance(data["rooming"], dict):
        raise ValueError("rooming 必须是对象")
    if isinstance(data.get("rooming"), dict) and data["rooming"].get("assignments") is not None and not isinstance(data["rooming"]["assignments"], dict):
        raise ValueError("assignments 必须是成员到房间的映射")
    members = [member_from_dict(m, i) for i, m in enumerate(raw_members)]

    for value in (data.get("nights"), (data.get("rooming") or {}).get("rooms"), (data.get("rooming") or {}).get("nights"), data.get("arrival_buffer_minutes"), data.get("departure_buffer_minutes"), data.get("home_buffer_minutes")):
        if value is not None and (isinstance(value, bool) or not math.isfinite(float(value)) or float(value) != int(float(value))):
            raise ValueError("晚数、房间数与缓冲分钟必须是整数")
    nights = data.get("nights")
    if nights is not None:
        nights = int(nights)

    for m in raw_members:
        for key in ("budget", "max_oneway_minutes", "max_roundtrip_minutes", "max_oneway_hours", "max_roundtrip_hours"):
            if m.get(key) not in (None, "") and (isinstance(m[key], bool) or not math.isfinite(float(m[key]))):
                raise ValueError(f"{key} 必须是有限数值")
    for key in ("team_budget", "min_common_hours"):
        if data.get(key) not in (None, "") and (isinstance(data[key], bool) or not math.isfinite(float(data[key]))):
            raise ValueError(f"{key} 必须是有限数值")
    rooming = rooming_from_dict(data.get("rooming"), nights)
    if rooming and nights is None:
        nights = rooming.nights
    if nights is None and rooming is None:
        nights = None

    today = _parse_date(data.get("today"))
    min_common = data.get("min_common_hours")

    return TripRequest(
        members=members,
        candidates=[str(c) for c in (data.get("candidates") or [])],
        team_budget=float(data["team_budget"]) if data.get("team_budget") not in (None, "") else None,
        rooming=rooming,
        nights=nights,
        min_common_hours=float(min_common) if min_common not in (None, "") else 12.0,
        today=today,
        hotel_poi=data.get("hotel_poi"),
        hotel_quote=data.get("hotel_quote"),
        arrival_buffer_minutes=int(data.get("arrival_buffer_minutes", 60)),
        departure_buffer_minutes=int(data.get("departure_buffer_minutes", 90)),
        home_buffer_minutes=int(data.get("home_buffer_minutes", 60)),
    )


def request_from_json(text: Union[str, Path]) -> TripRequest:
    if isinstance(text, Path) or (isinstance(text, str) and text.strip().endswith(".json")
                                  and "\n" not in text and len(text) < 4096):
        p = Path(text)
        if p.exists():
            text = p.read_text(encoding="utf-8")
    return request_from_dict(json.loads(text))


def request_to_dict(req: TripRequest) -> Dict[str, Any]:
    """回写为 JSON，便于缓存到文件实现增量重算。"""
    return {
        "members": [
            {
                "name": m.name,
                "origin_city": m.origin_city,
                "dep_date_start": m.dep_date_start.isoformat() if m.dep_date_start else None,
                "dep_date_end": m.dep_date_end.isoformat() if m.dep_date_end else None,
                "ret_date_start": m.ret_date_start.isoformat() if m.ret_date_start else None,
                "ret_date_end": m.ret_date_end.isoformat() if m.ret_date_end else None,
                "back_home_by": m.back_home_by.isoformat(sep=" ") if m.back_home_by else None,
                "budget": m.budget,
                "max_oneway_minutes": m.max_oneway_minutes,
                "max_roundtrip_minutes": m.max_roundtrip_minutes,
                "modes": m.modes,
                "notes": m.notes,
            }
            for m in req.members
        ],
        "candidates": req.candidates,
        "team_budget": req.team_budget,
        "nights": req.nights,
        "min_common_hours": req.min_common_hours,
        "hotel_poi": req.hotel_poi,
        "hotel_quote": req.hotel_quote,
        "arrival_buffer_minutes": req.arrival_buffer_minutes,
        "departure_buffer_minutes": req.departure_buffer_minutes,
        "home_buffer_minutes": req.home_buffer_minutes,
        "today": req.today.isoformat() if req.today else None,
        "rooming": {
            "rooms": req.rooming.rooms,
            "nights": req.rooming.nights,
            "assignments": req.rooming.assignments,
            "note": req.rooming.note,
        } if req.rooming else None,
    }
