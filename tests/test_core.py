from __future__ import annotations

import json
import unittest
import uuid
from datetime import date
from pathlib import Path

from scheduler.calendar_utils import date_to_week, parse_week_spec
from scheduler.importer import ImportManager
from scheduler.llm import NaturalLanguageParser
from scheduler.pdf_parser import extract_schedule
from scheduler.storage import Database
from scheduler.web import _is_client_ip_allowed, _is_valid_ip_allowlist, _render_home_page


ROOT = Path(__file__).resolve().parent.parent


class CalendarUtilsTest(unittest.TestCase):
    def test_parse_week_spec(self) -> None:
        self.assertEqual(parse_week_spec("2-4周(双),8周,12-16周(双)"), [2, 4, 8, 12, 14, 16])

    def test_date_to_week(self) -> None:
        self.assertEqual(date_to_week(date(2026, 3, 2), date(2026, 3, 11)), 2)


class PdfParserTest(unittest.TestCase):
    def test_extract_university_physics_lab(self) -> None:
        schedule = extract_schedule(ROOT / "原振国(2025-2026-2)课表.pdf")
        target = next(
            meeting
            for meeting in schedule.meetings
            if meeting.course_name == "大学物理实验C"
        )
        self.assertEqual(target.weekday, 1)
        self.assertEqual((target.period_start, target.period_end), (6, 8))
        self.assertEqual(target.weeks[0], 2)
        self.assertEqual(target.weeks[-1], 17)


class NaturalLanguageParserTest(unittest.TestCase):
    def test_parse_month_day_phrase(self) -> None:
        parser = NaturalLanguageParser()
        parsed = parser.parse(
            question="4月2日下午谁有空",
            today=date(2026, 3, 11),
            current_week=2,
            semester_start=date(2026, 3, 2),
            people_names=["原振国", "周子睿"],
        )
        self.assertEqual(parsed["date"], "2026-04-02")
        self.assertEqual(parsed["scope"], "afternoon")
        self.assertEqual(parsed["weekday"], 4)


class ImportFlowTest(unittest.TestCase):
    def test_scan_and_confirm(self) -> None:
        state_dir = ROOT / ".test_state"
        state_dir.mkdir(exist_ok=True)
        db_path = state_dir / f"schedule-{uuid.uuid4().hex}.db"
        db = Database(db_path)
        db.initialize(default_admin_password="admin", default_semester_start="2026-03-02", watch_dir=str(ROOT))
        manager = ImportManager(db=db, pdf_dir=ROOT, poll_interval=5)
        result = manager.scan_once()
        self.assertIn("import_id", result)
        imports = db.list_imports()
        self.assertGreaterEqual(len(imports), 1)
        db.confirm_import(int(imports[0]["id"]))
        current = db.get_current_import()
        self.assertIsNotNone(current)
        availability = db.compute_availability(
            week=2,
            weekday=1,
            scope="afternoon",
            semester_start=db.resolve_semester_start(),
        )
        self.assertTrue(availability["has_data"])
        self.assertIn("groups", availability["summary"])
        self.assertIn("partial", availability["summary"]["groups"])
        period_detail = next(item for item in availability["period_details"] if item["period"] == 6)
        self.assertEqual(period_detail["time"], "13:30-14:15")
        lab_owner = next(
            person
            for person in availability["people"]
            if any(slot["course_name"] == "大学物理实验C" for slot in person["slots"])
        )
        lab_slot = next(slot for slot in lab_owner["slots"] if slot["course_name"] == "大学物理实验C")
        self.assertEqual(lab_slot["weeks_text"], "第2-17周")
        self.assertEqual(lab_slot["course_time_text"], "13:30-16:00")
        config = db.get_config()
        self.assertEqual(config["public_nl_remote_fallback"], "0")
        self.assertEqual(config["admin_trusted_ips"], "")
        filtered = db.compute_availability(
            week=2,
            weekday=1,
            scope="afternoon",
            semester_start=db.resolve_semester_start(),
            selected_people=["徐谦", "原振国"],
            selection_mode="custom",
        )
        self.assertEqual(filtered["people_mode"], "custom")
        self.assertEqual(filtered["selected_people"], ["徐谦", "原振国"])
        self.assertEqual([person["person_name"] for person in filtered["people"]], ["徐谦", "原振国"])
        self.assertEqual(filtered["summary"]["counts"]["selected"], 2)
        self.assertEqual([person["busy_count"] for person in filtered["people"]], sorted(person["busy_count"] for person in filtered["people"]))
        self.assertEqual(filtered["collaboration"]["total_people"], 2)
        self.assertEqual(len(filtered["collaboration"]["heatmap"]), 5)
        self.assertTrue(all(row["weekday"] <= 5 for row in filtered["collaboration"]["heatmap"]))
        self.assertEqual(len(filtered["collaboration"]["rankings"]), 5)
        self.assertTrue(
            all(
                item["total_count"] <= 2
                for row in filtered["collaboration"]["heatmap"]
                for item in row["items"]
            )
        )


class SecurityHelpersTest(unittest.TestCase):
    def test_ip_allowlist(self) -> None:
        self.assertTrue(_is_client_ip_allowed("127.0.0.1", ""))
        self.assertTrue(_is_client_ip_allowed("192.168.1.10", "192.168.1.0/24"))
        self.assertTrue(_is_client_ip_allowed("192.168.1.10", "10.0.0.1, 192.168.1.10"))
        self.assertFalse(_is_client_ip_allowed("192.168.2.10", "192.168.1.0/24"))
        self.assertTrue(_is_valid_ip_allowlist("127.0.0.1,192.168.1.0/24"))
        self.assertFalse(_is_valid_ip_allowlist("127.0.0.1,not-an-ip"))


class UiRenderSmokeTest(unittest.TestCase):
    def test_home_page_contains_collaboration_modules(self) -> None:
        db = Database(ROOT / ".schedule_state" / "schedule.db")
        current_import = db.get_current_import()
        self.assertIsNotNone(current_import)
        today = date(2026, 3, 11)
        current_context = db.describe_current_date_context(today)
        availability = db.compute_availability(
            week=2,
            weekday=3,
            scope="all_day",
            semester_start=db.resolve_semester_start(),
            requested_date=today,
        )
        people_names = [person["person_name"] for person in db.get_current_people()]
        page = _render_home_page(db.get_config(), availability, current_context, people_names)
        self.assertIn("人员筛选", page)
        self.assertIn("协同洞察", page)
        self.assertIn("本周热力图", page)
        self.assertIn("人最齐时段 Top 5", page)
        self.assertIn("自然语言查询", page)

    def test_static_export_omits_secrets(self) -> None:
        db = Database(ROOT / ".schedule_state" / "schedule.db")
        payload = db.export_static_dataset(today=date(2026, 3, 11))
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertIn("semester_start_date", payload)
        self.assertIn("period_details", payload)
        self.assertIn("meetings", payload)
        self.assertEqual(payload["period_details"][0]["time"], "08:05-08:50")
        self.assertNotIn("llm_api_key", serialized)
        self.assertNotIn("admin_password_hash", serialized)


if __name__ == "__main__":
    unittest.main()
