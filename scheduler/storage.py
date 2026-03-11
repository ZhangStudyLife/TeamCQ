from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .calendar_utils import ALL_PERIODS, date_to_week, scope_label, scope_periods, week_day_to_date, weekday_label
from .models import ParsedSchedule


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


class Database:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self._lock = threading.RLock()
        self._state = self._load_state()

    def initialize(self, default_admin_password: str, default_semester_start: str, watch_dir: str) -> None:
        with self._lock:
            config = self._state["config"]
            self._ensure_default(config, "semester_start_date", default_semester_start)
            self._ensure_default(config, "watch_dir", watch_dir)
            self._ensure_default(config, "llm_base_url", "https://open.bigmodel.cn/api/paas/v4/")
            self._ensure_default(config, "llm_api_key", "")
            self._ensure_default(config, "llm_model", "glm-5")
            self._ensure_default(config, "vision_model", "glm-ocr")
            self._ensure_default(config, "pdf_parser_tool_type", "expert")
            self._ensure_default(config, "public_nl_remote_fallback", "0")
            self._ensure_default(config, "admin_trusted_ips", "")
            self._ensure_default(config, "admin_password_hash", _hash_password(default_admin_password))
            self._save_state()

    @staticmethod
    def _ensure_default(config: dict[str, str], key: str, value: str) -> None:
        if not config.get(key):
            config[key] = value

    def get_config(self) -> dict[str, str]:
        with self._lock:
            return dict(self._state["config"])

    def update_config(self, updates: dict[str, str]) -> None:
        with self._lock:
            self._state["config"].update(updates)
            self._save_state()

    def verify_admin_password(self, password: str) -> bool:
        return hmac.compare_digest(self.get_config().get("admin_password_hash", ""), _hash_password(password))

    def set_admin_password(self, password: str) -> None:
        self.update_config({"admin_password_hash": _hash_password(password)})

    def get_import_by_hash(self, dataset_hash: str) -> dict[str, Any] | None:
        with self._lock:
            for item in self._state["imports"]:
                if item["dataset_hash"] == dataset_hash:
                    return self._import_metadata(item)
        return None

    def save_draft_import(self, dataset_hash: str, schedules: list[ParsedSchedule], warnings: list[str]) -> int:
        with self._lock:
            import_id = int(self._state["next_import_id"])
            self._state["next_import_id"] += 1
            import_entry = {
                "id": import_id,
                "dataset_hash": dataset_hash,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "status": "draft",
                "source_count": len(schedules),
                "warning_count": len(warnings),
                "warnings": list(warnings),
                "files": [
                    {
                        "person_name": schedule.person_name,
                        "student_id": schedule.student_id,
                        "source_file": schedule.source_file,
                        "file_hash": schedule.file_hash,
                        "modified_at": schedule.modified_at.isoformat(timespec="seconds"),
                    }
                    for schedule in schedules
                ],
                "meetings": [
                    {
                        "person_name": meeting.person_name,
                        "student_id": meeting.student_id,
                        "course_name": meeting.course_name,
                        "weekday": meeting.weekday,
                        "period_start": meeting.period_start,
                        "period_end": meeting.period_end,
                        "weeks": list(meeting.weeks),
                        "location": meeting.location,
                        "teacher": meeting.teacher,
                        "raw_detail": meeting.raw_detail,
                        "confidence": meeting.confidence,
                        "warning": meeting.warning,
                    }
                    for schedule in schedules
                    for meeting in schedule.meetings
                ],
            }
            self._state["imports"].append(import_entry)
            self._save_state()
            return import_id

    def confirm_import(self, import_id: int) -> None:
        with self._lock:
            for item in self._state["imports"]:
                if item["status"] == "current":
                    item["status"] = "archived"
                if item["id"] == import_id:
                    item["status"] = "current"
            self._save_state()

    def list_imports(self) -> list[dict[str, Any]]:
        with self._lock:
            items = [self._import_metadata(item) for item in self._state["imports"]]
        return list(reversed(items))

    def get_import_detail(self, import_id: int) -> dict[str, Any] | None:
        with self._lock:
            item = self._find_import(import_id)
            if not item:
                return None
            return {
                "import": self._import_metadata(item),
                "files": deepcopy(item["files"]),
                "meetings": deepcopy(item["meetings"]),
                "warnings": list(item["warnings"]),
            }

    def get_current_import(self) -> dict[str, Any] | None:
        with self._lock:
            current = next((item for item in reversed(self._state["imports"]) if item["status"] == "current"), None)
            return self._import_metadata(current) if current else None

    def get_people(self, import_id: int) -> list[dict[str, str]]:
        with self._lock:
            item = self._find_import(import_id)
            if not item:
                return []
            people = {}
            for file_entry in item["files"]:
                people[file_entry["person_name"]] = {
                    "person_name": file_entry["person_name"],
                    "student_id": file_entry["student_id"],
                }
        return [people[name] for name in sorted(people)]

    def get_current_people(self) -> list[dict[str, str]]:
        current = self.get_current_import()
        return self.get_people(int(current["id"])) if current else []

    @staticmethod
    def _select_people(
        people: list[dict[str, str]],
        selected_people: list[str] | None,
        selection_mode: str,
    ) -> list[dict[str, str]]:
        if selection_mode != "custom":
            return list(people)
        names = {person["person_name"]: person for person in people}
        ordered: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw_name in selected_people or []:
            name = raw_name.strip()
            if not name or name in seen or name not in names:
                continue
            ordered.append(names[name])
            seen.add(name)
        return ordered

    @staticmethod
    def _build_scope_snapshot(
        people: list[dict[str, str]],
        meetings: list[dict[str, Any]],
        week: int,
        weekday: int,
        scope: str,
    ) -> dict[str, Any]:
        periods = list(scope_periods(scope))
        meetings_by_person: dict[str, list[dict[str, Any]]] = {}
        for meeting in meetings:
            if meeting["weekday"] != weekday:
                continue
            if week not in meeting["weeks"]:
                continue
            meetings_by_person.setdefault(meeting["person_name"], []).append(meeting)

        people_result: list[dict[str, Any]] = []
        free_people: list[str] = []
        busy_people: list[str] = []
        partial_people: list[str] = []
        for person in people:
            slot_map = {period: None for period in ALL_PERIODS}
            for meeting in meetings_by_person.get(person["person_name"], []):
                for period in range(meeting["period_start"], meeting["period_end"] + 1):
                    slot_map[period] = meeting
            scoped_slots = []
            for period in periods:
                meeting = slot_map[period]
                scoped_slots.append(
                    {
                        "period": period,
                        "status": "busy" if meeting else "free",
                        "period_time": _period_time_text(period),
                        "course_time_text": _meeting_time_text(meeting["period_start"], meeting["period_end"]) if meeting else _period_time_text(period),
                        "course_name": meeting["course_name"] if meeting else "",
                        "location": meeting["location"] if meeting else "",
                        "teacher": meeting["teacher"] if meeting else "",
                        "course_code": _extract_course_code(meeting["raw_detail"]) if meeting else "",
                        "weeks_text": _summarize_weeks(meeting["weeks"]) if meeting else "",
                        "raw_detail": meeting["raw_detail"] if meeting else "",
                        "period_start": meeting["period_start"] if meeting else period,
                        "period_end": meeting["period_end"] if meeting else period,
                    }
                )
            free_count = sum(1 for slot in scoped_slots if slot["status"] == "free")
            busy_count = len(scoped_slots) - free_count
            if busy_count == 0:
                availability_status = "all_free"
            elif free_count == 0:
                availability_status = "all_busy"
            else:
                availability_status = "partial"
            people_result.append(
                {
                    "person_name": person["person_name"],
                    "student_id": person["student_id"],
                    "slots": scoped_slots,
                    "is_free": availability_status == "all_free",
                    "availability_status": availability_status,
                    "free_count": free_count,
                    "busy_count": busy_count,
                    "busy_meetings": meetings_by_person.get(person["person_name"], []),
                }
            )
            if availability_status == "all_free":
                free_people.append(person["person_name"])
            elif availability_status == "all_busy":
                busy_people.append(person["person_name"])
            else:
                partial_people.append(person["person_name"])

        people_result.sort(key=lambda item: (item["busy_count"], item["person_name"]))
        return {
            "people": people_result,
            "summary": {
                "free_people": free_people,
                "busy_people": busy_people,
                "partial_people": partial_people,
                "groups": {
                    "all_free": free_people,
                    "all_busy": busy_people,
                    "partial": partial_people,
                },
                "counts": {
                    "selected": len(people_result),
                    "all_free": len(free_people),
                    "all_busy": len(busy_people),
                    "partial": len(partial_people),
                },
            },
            "periods": periods,
            "period_details": [_build_period_detail(period) for period in periods],
        }

    def _build_collaboration(
        self,
        people: list[dict[str, str]],
        meetings: list[dict[str, Any]],
        week: int,
        semester_start: date,
    ) -> dict[str, Any]:
        scopes = ("morning", "afternoon", "evening")
        scope_order = {scope: index for index, scope in enumerate(scopes)}
        heatmap: list[dict[str, Any]] = []
        rankings: list[dict[str, Any]] = []
        for weekday in range(1, 6):
            items: list[dict[str, Any]] = []
            for scope in scopes:
                snapshot = self._build_scope_snapshot(people, meetings, week, weekday, scope)
                groups = snapshot["summary"]["groups"]
                item = {
                    "week": week,
                    "weekday": weekday,
                    "weekday_label": weekday_label(weekday),
                    "scope": scope,
                    "scope_label": scope_label(scope),
                    "date": week_day_to_date(semester_start, max(1, week), weekday).isoformat(),
                    "total_count": len(snapshot["people"]),
                    "free_count": len(groups["all_free"]),
                    "partial_count": len(groups["partial"]),
                    "busy_count": len(groups["all_busy"]),
                    "free_people": list(groups["all_free"]),
                    "partial_people": list(groups["partial"]),
                    "busy_people": list(groups["all_busy"]),
                }
                items.append(item)
                rankings.append(item)
            heatmap.append(
                {
                    "weekday": weekday,
                    "weekday_label": weekday_label(weekday),
                    "items": items,
                }
            )
        rankings.sort(
            key=lambda item: (
                -item["free_count"],
                -item["partial_count"],
                item["weekday"],
                scope_order[item["scope"]],
            )
        )
        return {
            "week": week,
            "total_people": len(people),
            "heatmap": heatmap,
            "rankings": rankings[:5],
        }

    def compute_availability(
        self,
        week: int,
        weekday: int,
        scope: str,
        semester_start: date,
        requested_date: date | None = None,
        selected_people: list[str] | None = None,
        selection_mode: str = "all",
    ) -> dict[str, Any]:
        current = self.get_current_import()
        resolved_date = requested_date or week_day_to_date(semester_start, max(1, week), weekday)
        if not current:
            periods = list(scope_periods(scope))
            return {
                "meta": {
                    "week": week,
                    "weekday": weekday,
                    "weekday_label": weekday_label(weekday),
                    "scope": scope,
                    "scope_label": scope_label(scope),
                    "date": resolved_date.isoformat(),
                },
                "people": [],
                "summary": {
                    "free_people": [],
                    "busy_people": [],
                    "partial_people": [],
                    "groups": {"all_free": [], "all_busy": [], "partial": []},
                },
                "periods": periods,
                "period_details": [_build_period_detail(period) for period in periods],
                "selected_people": list(dict.fromkeys(selected_people or [])) if selection_mode == "custom" else [],
                "available_people": [],
                "people_mode": selection_mode,
                "collaboration": {"week": week, "total_people": 0, "heatmap": [], "rankings": []},
                "has_data": False,
            }

        import_id = int(current["id"])
        all_people = self.get_people(import_id)
        people = self._select_people(all_people, selected_people, selection_mode)
        selected_names = [person["person_name"] for person in people]
        import_detail = self.get_import_detail(import_id)
        meetings = import_detail["meetings"] if import_detail else []
        snapshot = self._build_scope_snapshot(people, meetings, week, weekday, scope)
        return {
            "meta": {
                "week": week,
                "weekday": weekday,
                "weekday_label": weekday_label(weekday),
                "scope": scope,
                "scope_label": scope_label(scope),
                "date": resolved_date.isoformat(),
                "current_import_id": import_id,
                "current_import_created_at": current["created_at"],
            },
            "people": snapshot["people"],
            "summary": snapshot["summary"],
            "periods": snapshot["periods"],
            "period_details": snapshot["period_details"],
            "selected_people": selected_names,
            "available_people": [person["person_name"] for person in all_people],
            "people_mode": selection_mode,
            "collaboration": self._build_collaboration(people, meetings, week, semester_start),
            "has_data": True,
        }

    def resolve_semester_start(self) -> date:
        raw = self.get_config().get("semester_start_date", "2026-03-02")
        return date.fromisoformat(raw)

    def describe_current_date_context(self, today: date) -> dict[str, Any]:
        semester_start = self.resolve_semester_start()
        return {"today": today.isoformat(), "week": date_to_week(semester_start, today), "weekday": today.isoweekday()}

    def export_static_dataset(self, today: date | None = None) -> dict[str, Any]:
        current = self.get_current_import()
        if not current:
            raise RuntimeError("当前没有已生效课表，无法导出 GitHub Pages 数据")

        export_today = today or date.today()
        semester_start = self.resolve_semester_start()
        current_context = self.describe_current_date_context(export_today)
        import_id = int(current["id"])
        people = self.get_people(import_id)
        import_detail = self.get_import_detail(import_id)
        meetings = import_detail["meetings"] if import_detail else []

        exported_meetings: list[dict[str, Any]] = []
        max_week = 0
        for meeting in meetings:
            weeks = sorted({int(week) for week in meeting.get("weeks", [])})
            if weeks:
                max_week = max(max_week, weeks[-1])
            exported_meetings.append(
                {
                    "person_name": meeting["person_name"],
                    "student_id": meeting["student_id"],
                    "course_name": meeting["course_name"],
                    "weekday": int(meeting["weekday"]),
                    "period_start": int(meeting["period_start"]),
                    "period_end": int(meeting["period_end"]),
                    "weeks": weeks,
                    "weeks_text": _summarize_weeks(weeks),
                    "location": meeting.get("location", ""),
                    "teacher": meeting.get("teacher", ""),
                    "course_code": _extract_course_code(meeting.get("raw_detail", "")),
                    "course_time_text": _meeting_time_text(int(meeting["period_start"]), int(meeting["period_end"])),
                    "warning": meeting.get("warning", ""),
                    "confidence": float(meeting.get("confidence", 1.0)),
                }
            )

        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "semester_start_date": semester_start.isoformat(),
            "today": export_today.isoformat(),
            "current_week": current_context["week"],
            "current_weekday": current_context["weekday"],
            "current_import": current,
            "max_week": max_week,
            "people": people,
            "period_details": [_build_period_detail(period) for period in ALL_PERIODS],
            "meetings": exported_meetings,
        }

    def _find_import(self, import_id: int) -> dict[str, Any] | None:
        return next((item for item in self._state["imports"] if item["id"] == import_id), None)

    @staticmethod
    def _import_metadata(item: dict[str, Any] | None) -> dict[str, Any] | None:
        if not item:
            return None
        return {
            "id": item["id"],
            "dataset_hash": item["dataset_hash"],
            "created_at": item["created_at"],
            "status": item["status"],
            "source_count": item["source_count"],
            "warning_count": item["warning_count"],
        }

    def _load_state(self) -> dict[str, Any]:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {"config": {}, "imports": [], "next_import_id": 1}

    def _save_state(self) -> None:
        self._path.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")


def _extract_course_code(raw_detail: str) -> str:
    patterns = [
        r"教学班[:：]\((?:\d{4}-\d{4}-\d)\)-([A-Z]\d+(?:-\d+)?)",
        r"教学班[:：]([A-Z]\d+(?:-\d+)?)",
        r"\b([A-Z]\d{6,}(?:-\d+)?)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw_detail)
        if match:
            return match.group(1)
    return ""


def _summarize_weeks(weeks: list[int]) -> str:
    ordered = sorted({int(week) for week in weeks})
    if not ordered:
        return ""

    parts: list[str] = []
    index = 0
    while index < len(ordered):
        start = ordered[index]

        end = start
        while index + 1 < len(ordered) and ordered[index + 1] == end + 1:
            index += 1
            end = ordered[index]
        if end != start:
            parts.append(f"第{start}-{end}周")
            index += 1
            continue

        end = start
        parity = start % 2
        parity_count = 1
        while index + 1 < len(ordered) and ordered[index + 1] == end + 2 and ordered[index + 1] % 2 == parity:
            index += 1
            end = ordered[index]
            parity_count += 1
        if parity_count >= 3:
            mode = "单" if parity == 1 else "双"
            parts.append(f"第{start}-{end}周({mode})")
        else:
            parts.extend(f"第{start + offset * 2}周" for offset in range(parity_count))
        index += 1

    return "、".join(parts)


def _build_period_detail(period: int) -> dict[str, str | int]:
    return {
        "period": period,
        "label": f"第{period}节",
        "time": _period_time_text(period),
    }


def _period_time_text(period: int) -> str:
    mapping = {
        1: "08:05-08:50",
        2: "08:55-09:40",
        3: "10:00-10:45",
        4: "10:50-11:35",
        5: "11:40-12:25",
        6: "13:30-14:15",
        7: "14:20-15:05",
        8: "15:15-16:00",
        9: "16:05-16:50",
        10: "18:30-19:15",
        11: "19:20-20:05",
        12: "20:10-20:55",
    }
    return mapping.get(period, "")


def _meeting_time_text(period_start: int, period_end: int) -> str:
    start_text = _period_time_text(period_start)
    end_text = _period_time_text(period_end)
    if not start_text and not end_text:
        return ""
    start_value = start_text.split("-", 1)[0] if start_text else ""
    end_value = end_text.rsplit("-", 1)[-1] if end_text else ""
    if start_value and end_value:
        return f"{start_value}-{end_value}"
    return start_text or end_text
