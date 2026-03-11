from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import date, timedelta
from typing import Any

from .calendar_utils import WEEKDAY_ALIAS, date_to_week, parse_date_input


class NaturalLanguageParser:
    def __init__(self, base_url: str = "", api_key: str = "", model: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model

    def parse(
        self,
        question: str,
        today: date,
        current_week: int,
        semester_start: date,
        people_names: list[str],
    ) -> dict[str, Any]:
        local = self._parse_locally(question, today, current_week, semester_start, people_names)
        if self._is_complete(local):
            return local
        remote = self._parse_remotely(question, today, current_week, semester_start, people_names)
        return remote or local

    def _parse_locally(
        self,
        question: str,
        today: date,
        current_week: int,
        semester_start: date,
        people_names: list[str],
    ) -> dict[str, Any]:
        normalized = question.strip()
        result: dict[str, Any] = {
            "question": normalized,
            "scope": "all_day",
            "people_names": [name for name in people_names if name and name in normalized],
            "source": "local",
        }

        if "上午" in normalized:
            result["scope"] = "morning"
        elif "下午" in normalized:
            result["scope"] = "afternoon"
        elif "晚上" in normalized:
            result["scope"] = "evening"
        elif "全天" in normalized or "从早到晚" in normalized:
            result["scope"] = "all_day"

        explicit_date = parse_date_input(normalized)
        if explicit_date is None:
            date_match = re.search(r"(20\d{2}[-/]?\d{2}[-/]?\d{2})", normalized)
            if date_match:
                explicit_date = parse_date_input(date_match.group(1))
        if explicit_date is None:
            month_day_match = re.search(r"(?:(20\d{2})年)?\s*(\d{1,2})月(\d{1,2})[日号]?", normalized)
            if month_day_match:
                year = int(month_day_match.group(1) or today.year)
                month = int(month_day_match.group(2))
                day = int(month_day_match.group(3))
                try:
                    explicit_date = date(year, month, day)
                except ValueError:
                    explicit_date = None
        if explicit_date:
            result["date"] = explicit_date.isoformat()
            result["week"] = date_to_week(semester_start, explicit_date)
            result["weekday"] = explicit_date.isoweekday()
            return result

        if "今天" in normalized:
            target_date = today
        elif "明天" in normalized:
            target_date = today + timedelta(days=1)
        elif "后天" in normalized:
            target_date = today + timedelta(days=2)
        else:
            target_date = None

        if target_date:
            result["date"] = target_date.isoformat()
            result["week"] = date_to_week(semester_start, target_date)
            result["weekday"] = target_date.isoweekday()
            return result

        week_match = re.search(r"第?\s*(\d+)\s*周", normalized)
        if week_match:
            result["week"] = int(week_match.group(1))
        elif "这周" in normalized or "本周" in normalized:
            result["week"] = current_week
        elif "下周" in normalized:
            result["week"] = current_week + 1
        elif "上周" in normalized:
            result["week"] = max(1, current_week - 1)

        weekday_match = re.search(r"(?:周|星期)([一二三四五六日天])", normalized)
        if weekday_match:
            result["weekday"] = WEEKDAY_ALIAS[weekday_match.group(1)]
        return result

    @staticmethod
    def _is_complete(payload: dict[str, Any]) -> bool:
        return bool(payload.get("week")) and bool(payload.get("weekday"))

    def _parse_remotely(
        self,
        question: str,
        today: date,
        current_week: int,
        semester_start: date,
        people_names: list[str],
    ) -> dict[str, Any] | None:
        if not self._base_url or not self._api_key or not self._model:
            return None
        payload = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是课表查询参数解析器，只返回 JSON 对象。"
                        "字段仅允许：week, weekday, date, scope, people_names。"
                        "scope 只允许 all_day/morning/afternoon/evening。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "question": question,
                            "today": today.isoformat(),
                            "current_week": current_week,
                            "semester_start": semester_start.isoformat(),
                            "people_names": people_names,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return None
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
        parsed = _extract_json(content)
        if not isinstance(parsed, dict):
            return None
        parsed["source"] = "llm"
        return parsed


def _extract_json(content: str) -> dict[str, Any] | None:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
