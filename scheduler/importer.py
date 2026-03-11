from __future__ import annotations

import hashlib
import threading
from datetime import datetime
from pathlib import Path

from .models import ParsedSchedule
from .pdf_parser import extract_schedule
from .storage import Database


class ImportManager:
    def __init__(self, db: Database, pdf_dir: Path, poll_interval: int = 15) -> None:
        self._db = db
        self._pdf_dir = pdf_dir
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="schedule-importer")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread:
            self._thread.join(timeout=3)

    def trigger_scan(self) -> None:
        self._wake_event.set()

    def scan_once(self) -> dict[str, object]:
        pdf_files = sorted(self._pdf_dir.glob("*.pdf"))
        if not pdf_files:
            return {"created": False, "warnings": ["目录中没有 PDF 文件"], "import_id": None}

        seen_hashes: dict[str, Path] = {}
        parsed: list[ParsedSchedule] = []
        warnings: list[str] = []

        for pdf_file in pdf_files:
            file_hash = hashlib.md5(pdf_file.read_bytes()).hexdigest()
            if file_hash in seen_hashes:
                warnings.append(f"忽略重复文件: {pdf_file.name} 与 {seen_hashes[file_hash].name} 内容一致")
                continue
            seen_hashes[file_hash] = pdf_file
            try:
                schedule = extract_schedule(pdf_file)
            except Exception as exc:
                warnings.append(f"解析失败: {pdf_file.name} -> {exc}")
                continue
            parsed.append(schedule)
            warnings.extend(schedule.warnings)

        selected = self._choose_latest_per_person(parsed, warnings)
        if not selected:
            return {"created": False, "warnings": warnings or ["没有可导入的课表"], "import_id": None}

        dataset_hash = self._build_dataset_hash(selected)
        existing = self._db.get_import_by_hash(dataset_hash)
        if existing:
            return {"created": False, "warnings": warnings, "import_id": existing["id"]}

        import_id = self._db.save_draft_import(dataset_hash, selected, warnings)
        return {"created": True, "warnings": warnings, "import_id": import_id}

    def _choose_latest_per_person(self, schedules: list[ParsedSchedule], warnings: list[str]) -> list[ParsedSchedule]:
        grouped: dict[str, list[ParsedSchedule]] = {}
        for schedule in schedules:
            grouped.setdefault(schedule.person_name, []).append(schedule)

        chosen: list[ParsedSchedule] = []
        for person_name, entries in grouped.items():
            entries.sort(key=lambda item: (item.modified_at, item.source_file), reverse=True)
            chosen.append(entries[0])
            if len(entries) > 1:
                dropped = ", ".join(item.source_file for item in entries[1:])
                warnings.append(f"{person_name} 存在多份课表，已选择最新文件 {entries[0].source_file}，忽略 {dropped}")
        chosen.sort(key=lambda item: item.person_name)
        return chosen

    @staticmethod
    def _build_dataset_hash(schedules: list[ParsedSchedule]) -> str:
        payload = "|".join(
            f"{schedule.person_name}:{schedule.student_id}:{schedule.file_hash}:{schedule.modified_at.isoformat(timespec='seconds')}"
            for schedule in sorted(schedules, key=lambda item: item.person_name)
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.scan_once()
            except Exception:
                pass
            self._wake_event.wait(self._poll_interval)
            self._wake_event.clear()
