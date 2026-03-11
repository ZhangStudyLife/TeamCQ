from __future__ import annotations

import re
from datetime import date, timedelta

MORNING_PERIODS = tuple(range(1, 6))
AFTERNOON_PERIODS = tuple(range(6, 10))
EVENING_PERIODS = tuple(range(10, 13))
ALL_PERIODS = tuple(range(1, 13))

WEEKDAY_LABELS = {
    "星期一": 1,
    "星期二": 2,
    "星期三": 3,
    "星期四": 4,
    "星期五": 5,
    "星期六": 6,
    "星期日": 7,
}

WEEKDAY_ALIAS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "日": 7,
    "天": 7,
}


def parse_period_range(text: str) -> tuple[int, int] | None:
    match = re.search(r"\((\d+)(?:-(\d+))?节\)", text)
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2) or match.group(1))
    return start, end


def parse_week_prefix(text: str) -> str | None:
    match = re.search(r"\((\d+(?:-\d+)?)节\)([^/]+)", text)
    if not match:
        return None
    return match.group(2).strip()


def parse_week_spec(spec: str) -> list[int]:
    cleaned = spec.replace("，", ",").replace(" ", "")
    weeks: set[int] = set()
    for chunk in filter(None, cleaned.split(",")):
        match = re.fullmatch(r"(\d+)(?:-(\d+))?周(?:\((单|双)\))?", chunk)
        if not match:
            continue
        start = int(match.group(1))
        end = int(match.group(2) or match.group(1))
        mode = match.group(3)
        for week in range(start, end + 1):
            if mode == "单" and week % 2 == 0:
                continue
            if mode == "双" and week % 2 == 1:
                continue
            weeks.add(week)
    return sorted(weeks)


def parse_date_input(text: str) -> date | None:
    text = text.strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return date.fromisoformat(text) if pattern == "%Y-%m-%d" and "-" in text else _parse_date(text, pattern)
        except ValueError:
            continue
    return None


def _parse_date(text: str, pattern: str) -> date:
    from datetime import datetime

    return datetime.strptime(text, pattern).date()


def date_to_week(semester_start: date, target: date) -> int:
    delta = (target - semester_start).days
    if delta < 0:
        return 0
    return delta // 7 + 1


def week_day_to_date(semester_start: date, week: int, weekday: int) -> date:
    return semester_start + timedelta(days=(week - 1) * 7 + weekday - 1)


def scope_periods(scope: str) -> tuple[int, ...]:
    normalized = scope or "all_day"
    if normalized == "morning":
        return MORNING_PERIODS
    if normalized == "afternoon":
        return AFTERNOON_PERIODS
    if normalized == "evening":
        return EVENING_PERIODS
    return ALL_PERIODS


def scope_label(scope: str) -> str:
    mapping = {
        "all_day": "全天",
        "morning": "上午",
        "afternoon": "下午",
        "evening": "晚上",
    }
    return mapping.get(scope or "all_day", "全天")


def weekday_label(value: int) -> str:
    reverse = {number: label for label, number in WEEKDAY_LABELS.items()}
    return reverse.get(value, f"星期{value}")

