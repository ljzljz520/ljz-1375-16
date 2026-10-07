"""时空判断：生效窗口（含跨日/跨夜）+ 班次窗口 + 方向。

window（绝对生效区间，可多个，支持跨多日）：
    [{"start": "2026-10-05T22:00", "end": "2026-10-08T06:00"}, ...]
weekly（周期班次，可跨夜）：
    {"days": [0,1,2,3,4,5,6], "start": "08:30", "end": "17:00",
     "cross_day": false, "date_start": "2026-03-01", "date_end": "2026-10-31"}
方向掩码 direction：'B' 双向 / 'F' 仅路线数字化方向 / 'R' 仅反方向。
"""
from datetime import datetime, timedelta

WEEK_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def parse_dt(s):
    if s is None:
        return None
    if isinstance(s, datetime):
        return s
    s = s.strip().replace("Z", "")
    if "T" not in s:
        s += "T00:00:00"
    if len(s) == 16:
        s += ":00"
    return datetime.fromisoformat(s)


def _hm(t):
    h, m = map(int, t.split(":")[:2])
    return h * 60 + m


def window_active(windows, t):
    """绝对时间窗：t 是否落在任一窗口内（天然支持跨多日）。"""
    t = parse_dt(t)
    for w in windows or []:
        if parse_dt(w["start"]) <= t <= parse_dt(w["end"]):
            return True
    return False


def weekly_active(weekly, t):
    """周期班次：处理跨夜与可选生效日期范围。"""
    if not weekly:
        return None  # 没有班次约束
    t = parse_dt(t)
    ds = weekly.get("date_start")
    de = weekly.get("date_end")
    if ds and t.date() < parse_dt(ds).date():
        return False
    if de and t.date() > parse_dt(de).date():
        return False
    days = weekly.get("days", list(range(7)))
    start = _hm(weekly["start"])
    end = _hm(weekly["end"])
    cur = t.hour * 60 + t.minute
    cross = weekly.get("cross_day", False) or end <= start
    wd = t.weekday()
    if not cross:
        return wd in days and start <= cur <= end
    # 跨夜：今夜属于"起始日"的班次
    if cur >= start and wd in days:
        return True
    if cur <= end and (wd - 1) % 7 in days:
        return True
    return False


def describe_windows(windows):
    out = []
    for w in windows or []:
        out.append(f"{w['start'].replace('T',' ')} 至 {w['end'].replace('T',' ')}")
    return "；".join(out)


def describe_weekly(weekly):
    if not weekly:
        return "无班次限制"
    days = weekly.get("days", list(range(7)))
    if len(days) == 7:
        d = "每日"
    else:
        d = "、".join(WEEK_CN[d] for d in sorted(days))
    x = "（跨夜）" if weekly.get("cross_day") else ""
    rng = ""
    if weekly.get("date_start") or weekly.get("date_end"):
        rng = f"，{weekly.get('date_start','?')}~{weekly.get('date_end','?')}"
    return f"{d} {weekly['start']}–{weekly['end']}{x}{rng}"


def direction_allows(mask, reverse):
    """mask='F' 只允许数字化方向；'R' 只允许反向；'B' 都允许。"""
    if mask in (None, "", "B"):
        return True
    if mask == "F":
        return not reverse
    if mask == "R":
        return reverse
    return True
