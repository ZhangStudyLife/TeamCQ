from __future__ import annotations

import hashlib
import re
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .calendar_utils import WEEKDAY_LABELS, parse_period_range, parse_week_prefix, parse_week_spec
from .models import CourseMeeting, ParsedSchedule

WHITESPACE = b" \t\r\n\x0c\x00"
DELIMITERS = b"[]<>()/{%}"
HEADER_TEXTS = {"时间段", "节次", "上午", "下午", "晚上", *WEEKDAY_LABELS.keys()}


@dataclass(slots=True)
class _TextItem:
    page_index: int
    sequence: int
    x: float
    y: float
    font_size: float | None
    text: str


def extract_schedule(pdf_path: Path) -> ParsedSchedule:
    data = pdf_path.read_bytes()
    page_streams = _parse_page_streams(data)
    text_items: list[_TextItem] = []
    for page_index, stream in enumerate(page_streams, start=1):
        text_items.extend(_parse_text_items(stream, page_index))

    person_name = _extract_person_name(text_items, pdf_path)
    student_id = _extract_student_id(text_items)
    weekday_columns = _extract_weekday_columns(text_items)
    meetings: list[CourseMeeting] = []
    warnings: list[str] = []

    for weekday in range(1, 8):
        blocks = _group_blocks_for_weekday(text_items, weekday_columns, weekday)
        for title, detail_text in blocks:
            period_range = parse_period_range(detail_text)
            week_prefix = parse_week_prefix(detail_text)
            if not period_range or not week_prefix:
                warnings.append(f"{person_name} {title} 未能完整解析节次或周次")
                continue
            weeks = parse_week_spec(week_prefix)
            if not weeks:
                warnings.append(f"{person_name} {title} 周次表达式无法识别: {week_prefix}")
                continue
            meetings.append(
                CourseMeeting(
                    person_name=person_name,
                    student_id=student_id,
                    course_name=title,
                    weekday=weekday,
                    period_start=period_range[0],
                    period_end=period_range[1],
                    weeks=weeks,
                    location=_extract_field(detail_text, "场地"),
                    teacher=_extract_field(detail_text, "教师"),
                    raw_detail=detail_text,
                    confidence=1.0,
                )
            )

    deduped_meetings = _dedupe_meetings(meetings)
    file_hash = hashlib.md5(data).hexdigest()
    return ParsedSchedule(
        person_name=person_name,
        student_id=student_id,
        source_file=pdf_path.name,
        file_hash=file_hash,
        modified_at=datetime.fromtimestamp(pdf_path.stat().st_mtime),
        meetings=deduped_meetings,
        warnings=warnings,
    )


def _parse_page_streams(data: bytes) -> list[bytes]:
    objects = _slice_objects(data)
    kids: list[int] = []
    for obj_bytes in objects.values():
        if b"/Type/Pages" not in obj_bytes:
            continue
        match = re.search(rb"/Kids\[(.*?)\]", obj_bytes, re.S)
        if not match:
            continue
        kids = [int(value) for value in re.findall(rb"(\d+)\s+0\s+R", match.group(1))]
        break
    streams: list[bytes] = []
    for page_id in kids:
        page_object = objects.get(page_id)
        if not page_object:
            continue
        match = re.search(rb"/Contents\s+(\d+)\s+0\s+R", page_object)
        if not match:
            continue
        content_id = int(match.group(1))
        content_object = objects.get(content_id)
        if not content_object:
            continue
        stream = _extract_stream(content_object)
        if stream is None:
            continue
        if b"/FlateDecode" in content_object:
            stream = zlib.decompress(stream)
        streams.append(stream)
    return streams


def _slice_objects(data: bytes) -> dict[int, bytes]:
    offsets = _parse_xref(data)
    ordered = sorted(offsets.items(), key=lambda item: item[1])
    xref_pos = data.rfind(b"xref")
    objects: dict[int, bytes] = {}
    for index, (obj_id, start) in enumerate(ordered):
        end = ordered[index + 1][1] if index + 1 < len(ordered) else xref_pos
        objects[obj_id] = data[start:end]
    return objects


def _parse_xref(data: bytes) -> dict[int, int]:
    startxref_pos = data.rfind(b"startxref")
    if startxref_pos < 0:
        raise ValueError("未找到 startxref")
    match = re.search(rb"startxref\s+(\d+)", data[startxref_pos:])
    if not match:
        raise ValueError("无法解析 xref 偏移")
    pos = int(match.group(1)) + 4
    offsets: dict[int, int] = {}
    while True:
        while pos < len(data) and data[pos] in WHITESPACE:
            pos += 1
        if data[pos : pos + 7] == b"trailer":
            break
        subsection = re.match(rb"(\d+)\s+(\d+)", data[pos:])
        if not subsection:
            raise ValueError("xref 子节格式错误")
        start_obj = int(subsection.group(1))
        count = int(subsection.group(2))
        pos += subsection.end()
        while pos < len(data) and data[pos] in WHITESPACE:
            pos += 1
        for index in range(count):
            line = data[pos : pos + 20]
            if len(line) < 18:
                break
            if chr(line[17]) == "n":
                offsets[start_obj + index] = int(line[:10])
            next_line = data.find(b"\n", pos)
            pos = len(data) if next_line < 0 else next_line + 1
    return offsets


def _extract_stream(obj_bytes: bytes) -> bytes | None:
    length_match = re.search(rb"/Length\s+(\d+)", obj_bytes)
    if not length_match:
        return None
    length = int(length_match.group(1))
    stream_pos = obj_bytes.find(b"stream")
    if stream_pos < 0:
        return None
    cursor = stream_pos + len(b"stream")
    if obj_bytes[cursor : cursor + 2] == b"\r\n":
        cursor += 2
    elif obj_bytes[cursor : cursor + 1] in (b"\r", b"\n"):
        cursor += 1
    return obj_bytes[cursor : cursor + length]


def _parse_text_items(stream: bytes, page_index: int) -> list[_TextItem]:
    items: list[_TextItem] = []
    stack: list[tuple[str, object]] = []
    position = (0.0, 0.0)
    font_size: float | None = None
    sequence = 0
    for kind, value in _tokenize(stream):
        if kind != "op":
            stack.append((kind, value))
            continue
        if value == "Tf":
            if len(stack) >= 2 and stack[-1][0] == "num":
                font_size = float(stack[-1][1])
            stack.clear()
            continue
        if value == "Tm":
            nums = [item[1] for item in stack[-6:] if item[0] == "num"]
            if len(nums) == 6:
                position = (float(nums[4]), float(nums[5]))
            stack.clear()
            continue
        if value == "Td":
            nums = [item[1] for item in stack[-2:] if item[0] == "num"]
            if len(nums) == 2:
                position = (position[0] + float(nums[0]), position[1] + float(nums[1]))
            stack.clear()
            continue
        if value == "Tj" and stack and stack[-1][0] == "string":
            text = _decode_pdf_text(stack[-1][1]).strip()
            if text:
                items.append(
                    _TextItem(
                        page_index=page_index,
                        sequence=sequence,
                        x=position[0],
                        y=position[1],
                        font_size=font_size,
                        text=text,
                    )
                )
                sequence += 1
            stack.clear()
            continue
        stack.clear()
    return items


def _tokenize(data: bytes):
    cursor = 0
    while cursor < len(data):
        char = data[cursor]
        if char in WHITESPACE:
            cursor += 1
            continue
        if char == 0x25:
            while cursor < len(data) and data[cursor] not in (0x0A, 0x0D):
                cursor += 1
            continue
        if char == 0x28:
            value, cursor = _parse_literal_string(data, cursor)
            yield "string", value
            continue
        if char == 0x2F:
            end = cursor + 1
            while end < len(data) and data[end] not in WHITESPACE + DELIMITERS:
                end += 1
            yield "name", data[cursor + 1 : end].decode("latin1", errors="ignore")
            cursor = end
            continue
        if char in b"[]":
            yield "sym", chr(char)
            cursor += 1
            continue
        end = cursor
        while end < len(data) and data[end] not in WHITESPACE + DELIMITERS:
            end += 1
        token = data[cursor:end].decode("latin1", errors="ignore")
        if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", token):
            yield "num", float(token)
        else:
            yield "op", token
        cursor = end


def _parse_literal_string(data: bytes, cursor: int) -> tuple[bytes, int]:
    cursor += 1
    depth = 1
    output = bytearray()
    while cursor < len(data):
        char = data[cursor]
        if char == 0x5C:
            output.append(char)
            cursor += 1
            if cursor < len(data):
                output.append(data[cursor])
                cursor += 1
            continue
        if char == 0x28:
            depth += 1
            output.append(char)
            cursor += 1
            continue
        if char == 0x29:
            depth -= 1
            if depth == 0:
                cursor += 1
                break
            output.append(char)
            cursor += 1
            continue
        output.append(char)
        cursor += 1
    return bytes(output), cursor


def _decode_pdf_text(raw: object) -> str:
    if not isinstance(raw, (bytes, bytearray)):
        return ""
    unescaped = _unescape_pdf_string(bytes(raw))
    try:
        return unescaped.decode("utf-16-be")
    except UnicodeDecodeError:
        return unescaped.decode("latin1", errors="ignore")


def _unescape_pdf_string(raw: bytes) -> bytes:
    output = bytearray()
    cursor = 0
    while cursor < len(raw):
        char = raw[cursor]
        if char == 0x5C and cursor + 1 < len(raw):
            cursor += 1
            escaped = raw[cursor]
            mapping = {
                ord("n"): 0x0A,
                ord("r"): 0x0D,
                ord("t"): 0x09,
                ord("b"): 0x08,
                ord("f"): 0x0C,
                ord("("): 0x28,
                ord(")"): 0x29,
                ord("\\"): 0x5C,
            }
            if escaped in mapping:
                output.append(mapping[escaped])
            elif 0x30 <= escaped <= 0x37:
                octal = bytes([escaped])
                read = 0
                while cursor + 1 < len(raw) and read < 2 and 0x30 <= raw[cursor + 1] <= 0x37:
                    cursor += 1
                    octal += bytes([raw[cursor]])
                    read += 1
                output.append(int(octal, 8))
            elif escaped in (0x0A, 0x0D):
                pass
            else:
                output.append(escaped)
        else:
            output.append(char)
        cursor += 1
    return bytes(output)


def _extract_person_name(text_items: list[_TextItem], pdf_path: Path) -> str:
    for item in text_items:
        if (item.font_size or 0) >= 20 and item.text.endswith("课表"):
            return re.sub(r"课表$", "", item.text)
    return pdf_path.stem.split("(")[0]


def _extract_student_id(text_items: list[_TextItem]) -> str:
    for item in text_items:
        match = re.search(r"学号[:：]\s*(\d+)", item.text)
        if match:
            return match.group(1)
    return ""


def _extract_weekday_columns(text_items: list[_TextItem]) -> list[tuple[float, int]]:
    columns: list[tuple[float, int]] = []
    for item in text_items:
        if item.text in WEEKDAY_LABELS:
            columns.append((item.x, WEEKDAY_LABELS[item.text]))
    columns.sort()
    return columns


def _group_blocks_for_weekday(
    text_items: list[_TextItem],
    weekday_columns: list[tuple[float, int]],
    weekday: int,
) -> list[tuple[str, str]]:
    def resolve_weekday(x_pos: float) -> int | None:
        if not weekday_columns:
            return None
        closest = min(weekday_columns, key=lambda item: abs(item[0] - x_pos))
        return closest[1] if abs(closest[0] - x_pos) < 40 else None

    items = [
        item
        for item in text_items
        if resolve_weekday(item.x) == weekday
        and item.text not in HEADER_TEXTS
        and not re.fullmatch(r"\d+", item.text)
        and "打印时间" not in item.text
        and not item.text.endswith("课表")
    ]
    items.sort(key=lambda item: (item.page_index, -item.y, item.sequence))

    blocks: list[dict[str, object]] = []
    current_block: dict[str, object] | None = None
    for item in items:
        is_title = (item.font_size or 0) >= 9 and ":" not in item.text and "：" not in item.text
        if is_title:
            if current_block and isinstance(current_block["details"], list) and not current_block["details"]:
                current_block["title"] = f"{current_block['title']}{item.text}"
                continue
            current_block = {"title": item.text, "details": []}
            blocks.append(current_block)
            continue
        if current_block is None:
            if blocks:
                cast_details = blocks[-1]["details"]
                if isinstance(cast_details, list):
                    cast_details.append(item.text)
            continue
        details = current_block["details"]
        if isinstance(details, list):
            details.append(item.text)

    merged: list[tuple[str, str]] = []
    for block in blocks:
        title = str(block["title"])
        detail_text = "".join(block["details"])  # type: ignore[arg-type]
        if merged and title == merged[-1][0] and parse_period_range(detail_text) is None:
            previous_title, previous_detail = merged[-1]
            merged[-1] = (previous_title, previous_detail + detail_text)
            continue
        merged.append((title, detail_text))
    return merged


def _extract_field(detail_text: str, label: str) -> str:
    match = re.search(fr"{label}[:：]([^/]+)", detail_text)
    return match.group(1).strip() if match else ""


def _dedupe_meetings(meetings: list[CourseMeeting]) -> list[CourseMeeting]:
    deduped: list[CourseMeeting] = []
    seen: set[tuple[object, ...]] = set()
    for meeting in meetings:
        key = (
            meeting.person_name,
            meeting.course_name,
            meeting.weekday,
            meeting.period_start,
            meeting.period_end,
            tuple(meeting.weeks),
            meeting.location,
            meeting.teacher,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(meeting)
    deduped.sort(key=lambda item: (item.person_name, item.weekday, item.period_start, item.course_name))
    return deduped
