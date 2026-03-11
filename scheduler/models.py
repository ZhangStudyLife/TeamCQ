from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class CourseMeeting:
    person_name: str
    student_id: str
    course_name: str
    weekday: int
    period_start: int
    period_end: int
    weeks: list[int]
    location: str
    teacher: str
    raw_detail: str
    confidence: float = 1.0
    warning: str = ""


@dataclass(slots=True)
class ParsedSchedule:
    person_name: str
    student_id: str
    source_file: str
    file_hash: str
    modified_at: datetime
    meetings: list[CourseMeeting]
    warnings: list[str] = field(default_factory=list)

