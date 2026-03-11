from __future__ import annotations

import html
import ipaddress
import json
import secrets
import threading
import time
from datetime import date, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from .calendar_utils import date_to_week, parse_date_input, scope_label, week_day_to_date, weekday_label
from .importer import ImportManager
from .llm import NaturalLanguageParser
from .storage import Database

MAX_FORM_BYTES = 16 * 1024
MAX_JSON_BYTES = 16 * 1024
MAX_NL_QUESTION_CHARS = 240
SESSION_TTL_SECONDS = 12 * 60 * 60
LOGIN_RATE_LIMIT = 5
LOGIN_RATE_WINDOW_SECONDS = 10 * 60
NL_RATE_LIMIT = 30
NL_RATE_WINDOW_SECONDS = 60
ADMIN_POST_RATE_LIMIT = 20
ADMIN_POST_RATE_WINDOW_SECONDS = 60


class RateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.time()
        with self._lock:
            valid = [value for value in self._events.get(key, []) if now - value < window_seconds]
            if len(valid) >= limit:
                self._events[key] = valid
                return False
            valid.append(now)
            self._events[key] = valid
            return True


class SessionStore:
    def __init__(self) -> None:
        self._tokens: dict[str, dict[str, object]] = {}
        self._lock = threading.Lock()

    def create(self, identity: str) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._tokens[token] = {
                "identity": identity,
                "csrf_token": secrets.token_urlsafe(24),
                "expires_at": time.time() + SESSION_TTL_SECONDS,
            }
        return token

    def validate(self, token: str | None) -> bool:
        return self.get(token) is not None

    def get(self, token: str | None) -> dict[str, object] | None:
        if not token:
            return None
        with self._lock:
            session = self._tokens.get(token)
            if not session:
                return None
            if float(session.get("expires_at", 0)) < time.time():
                self._tokens.pop(token, None)
                return None
            return dict(session)

    def get_csrf_token(self, token: str | None) -> str:
        session = self.get(token)
        if not session:
            return ""
        return str(session.get("csrf_token", ""))

    def delete(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._tokens.pop(token, None)


class AppContext:
    def __init__(self, db: Database, importer: ImportManager, parser: NaturalLanguageParser) -> None:
        self.db = db
        self.importer = importer
        self.nl_parser = parser
        self.sessions = SessionStore()
        self.login_limiter = RateLimiter()
        self.nl_limiter = RateLimiter()
        self.admin_post_limiter = RateLimiter()


def create_server(host: str, port: int, context: AppContext) -> ThreadingHTTPServer:
    class Handler(ScheduleRequestHandler):
        app_context = context

    return ThreadingHTTPServer((host, port), Handler)


class ScheduleRequestHandler(BaseHTTPRequestHandler):
    app_context: AppContext

    def log_message(self, format: str, *args) -> None:
        return

    def _client_ip(self) -> str:
        return str(self.client_address[0] or "")

    def _is_admin_ip_allowed(self) -> bool:
        config = self.app_context.db.get_config()
        return _is_client_ip_allowed(self._client_ip(), config.get("admin_trusted_ips", ""))

    def _send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")

    def _send_redirect(self, location: str, set_cookie: str | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self._send_security_headers()
        self.send_header("Location", location)
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()

    def _read_body(self, max_bytes: int) -> bytes | None:
        cached = getattr(self, "_cached_body", None)
        if cached is not None:
            return cached
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return None
        if length < 0 or length > max_bytes:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body too large")
            return None
        body = self.rfile.read(length)
        if len(body) > max_bytes:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body too large")
            return None
        self._cached_body = body
        return body

    def _verify_rate_limit(self, limiter: RateLimiter, key: str, limit: int, window_seconds: int, message: str) -> bool:
        if limiter.allow(key, limit, window_seconds):
            return True
        self._send_json({"error": message}, status=HTTPStatus.TOO_MANY_REQUESTS)
        return False

    def _csrf_token_for_request(self) -> str:
        return self.app_context.sessions.get_csrf_token(self._read_session_cookie())

    def do_GET(self) -> None:
        route = urlparse(self.path)
        if route.path == "/":
            self._handle_home(route)
            return
        if route.path == "/api/availability":
            self._handle_availability_api(route)
            return
        if route.path == "/admin/login":
            self._handle_login_page("")
            return
        if route.path == "/admin/imports":
            self._require_admin(self._handle_admin_page)
            return
        if route.path == "/healthz":
            self._send_json({"ok": True})
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_POST(self) -> None:
        route = urlparse(self.path)
        if route.path == "/api/query/nl":
            self._handle_natural_query()
            return
        if route.path == "/admin/login":
            self._handle_login_submit()
            return
        if route.path == "/admin/logout":
            self._require_admin(self._handle_logout, require_csrf=True)
            return
        if route.path == "/admin/rescan":
            self._require_admin(self._handle_rescan, require_csrf=True)
            return
        if route.path == "/admin/config":
            self._require_admin(self._handle_config_update, require_csrf=True)
            return
        if route.path.startswith("/admin/imports/") and route.path.endswith("/confirm"):
            self._require_admin(self._handle_confirm_import, require_csrf=True)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def _handle_home(self, route) -> None:
        today = date.today()
        config = self.app_context.db.get_config()
        semester_start = self.app_context.db.resolve_semester_start()
        current_context = self.app_context.db.describe_current_date_context(today)
        params = parse_qs(route.query)
        requested_date, week, weekday, scope = _resolve_query_request(params, current_context, semester_start)
        selected_people, people_mode = _resolve_people_selection(params)

        availability = self.app_context.db.compute_availability(
            week=week,
            weekday=weekday,
            scope=scope,
            semester_start=semester_start,
            requested_date=requested_date,
            selected_people=selected_people,
            selection_mode=people_mode,
        )
        people_names = [person["person_name"] for person in self.app_context.db.get_current_people()]
        body = _render_home_page(config, availability, current_context, people_names)
        self._send_html("课表空闲查询", body)

    def _handle_availability_api(self, route) -> None:
        today = date.today()
        semester_start = self.app_context.db.resolve_semester_start()
        current_context = self.app_context.db.describe_current_date_context(today)
        params = parse_qs(route.query)
        requested_date, week, weekday, scope = _resolve_query_request(params, current_context, semester_start)
        selected_people, people_mode = _resolve_people_selection(params)
        payload = self.app_context.db.compute_availability(
            week=week,
            weekday=weekday,
            scope=scope,
            semester_start=semester_start,
            requested_date=requested_date,
            selected_people=selected_people,
            selection_mode=people_mode,
        )
        self._send_json(payload)

    def _handle_natural_query(self) -> None:
        if not self._verify_rate_limit(
            self.app_context.nl_limiter,
            f"nl:{self._client_ip()}",
            NL_RATE_LIMIT,
            NL_RATE_WINDOW_SECONDS,
            "自然语言查询过于频繁，请稍后再试",
        ):
            return
        payload = self._read_json_or_form()
        if payload is None:
            return
        question = str(payload.get("question", "")).strip()
        if not question:
            self._send_json({"error": "缺少 question"}, status=HTTPStatus.BAD_REQUEST)
            return
        if len(question) > MAX_NL_QUESTION_CHARS:
            self._send_json({"error": f"question 过长，最多 {MAX_NL_QUESTION_CHARS} 个字符"}, status=HTTPStatus.BAD_REQUEST)
            return
        today = date.today()
        semester_start = self.app_context.db.resolve_semester_start()
        current_context = self.app_context.db.describe_current_date_context(today)
        all_people = [person["person_name"] for person in self.app_context.db.get_current_people()]
        raw_selected_people = payload.get("selected_people", [])
        if isinstance(raw_selected_people, list):
            selected_people = [str(item).strip() for item in raw_selected_people if str(item).strip()]
        elif isinstance(raw_selected_people, str):
            selected_people = [raw_selected_people.strip()] if raw_selected_people.strip() else []
        else:
            selected_people = []
        people_mode = str(payload.get("people_mode", "all") or "all")
        people_names = selected_people if people_mode == "custom" else all_people
        config = self.app_context.db.get_config()
        allow_remote_llm = self.app_context.sessions.validate(self._read_session_cookie()) or config.get("public_nl_remote_fallback", "0") == "1"
        parser = NaturalLanguageParser(
            base_url=config.get("llm_base_url", "") if allow_remote_llm else "",
            api_key=config.get("llm_api_key", "") if allow_remote_llm else "",
            model=config.get("llm_model", "") if allow_remote_llm else "",
        )
        parsed = parser.parse(
            question=question,
            today=today,
            current_week=current_context["week"],
            semester_start=semester_start,
            people_names=people_names,
        )
        requested_date = parse_date_input(str(parsed.get("date", ""))) if parsed.get("date") else None
        week = int(parsed.get("week") or (date_to_week(semester_start, requested_date) if requested_date else current_context["week"]))
        weekday = int(parsed.get("weekday") or (requested_date.isoweekday() if requested_date else current_context["weekday"]))
        scope = str(parsed.get("scope") or "all_day")
        availability = self.app_context.db.compute_availability(
            week=week,
            weekday=weekday,
            scope=scope,
            semester_start=semester_start,
            requested_date=requested_date,
            selected_people=selected_people,
            selection_mode=people_mode,
        )
        self._send_json(_build_natural_language_response(question, parsed, availability))

    def _handle_login_page(self, error_message: str) -> None:
        if not self._is_admin_ip_allowed():
            self.send_error(HTTPStatus.FORBIDDEN, "Admin access denied")
            return
        self._send_html("管理员登录", _render_login_page(error_message))

    def _handle_login_submit(self) -> None:
        if not self._is_admin_ip_allowed():
            self.send_error(HTTPStatus.FORBIDDEN, "Admin access denied")
            return
        if not self._verify_rate_limit(
            self.app_context.login_limiter,
            f"login:{self._client_ip()}",
            LOGIN_RATE_LIMIT,
            LOGIN_RATE_WINDOW_SECONDS,
            "登录尝试过于频繁，请稍后再试",
        ):
            return
        payload = self._read_form()
        if payload is None:
            return
        password = payload.get("password", [""])[0]
        if not self.app_context.db.verify_admin_password(password):
            self._handle_login_page("密码错误")
            return
        token = self.app_context.sessions.create("admin")
        self._send_redirect(
            "/admin/imports",
            set_cookie=f"schedule_admin_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL_SECONDS}",
        )

    def _handle_logout(self) -> None:
        token = self._read_session_cookie()
        self.app_context.sessions.delete(token)
        self._send_redirect("/", set_cookie="schedule_admin_session=deleted; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")

    def _handle_admin_page(self) -> None:
        imports = self.app_context.db.list_imports()
        details = [self.app_context.db.get_import_detail(int(item["id"])) for item in imports[:5]]
        self._send_html(
            "管理员后台",
            _render_admin_page(self.app_context.db.get_config(), details, self._csrf_token_for_request()),
        )

    def _handle_rescan(self) -> None:
        self.app_context.importer.trigger_scan()
        self.app_context.importer.scan_once()
        self._send_redirect("/admin/imports")

    def _handle_confirm_import(self) -> None:
        parts = [part for part in urlparse(self.path).path.split("/") if part]
        import_id = int(parts[2])
        self.app_context.db.confirm_import(import_id)
        self._send_redirect("/admin/imports")

    def _handle_config_update(self) -> None:
        payload = self._read_form()
        if payload is None:
            return
        current_config = self.app_context.db.get_config()
        admin_trusted_ips = payload.get("admin_trusted_ips", [""])[0].strip()
        if not _is_valid_ip_allowlist(admin_trusted_ips):
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid admin_trusted_ips")
            return
        updates = {
            "semester_start_date": payload.get("semester_start_date", ["2026-03-02"])[0],
            "llm_base_url": payload.get("llm_base_url", [""])[0].strip(),
            "llm_model": payload.get("llm_model", [""])[0].strip(),
            "vision_model": payload.get("vision_model", [""])[0].strip(),
            "pdf_parser_tool_type": payload.get("pdf_parser_tool_type", ["expert"])[0].strip(),
            "public_nl_remote_fallback": "1" if payload.get("public_nl_remote_fallback", [""])[0] == "1" else "0",
            "admin_trusted_ips": admin_trusted_ips,
        }
        llm_api_key = payload.get("llm_api_key", [""])[0].strip()
        updates["llm_api_key"] = llm_api_key or current_config.get("llm_api_key", "")
        self.app_context.db.update_config(updates)
        new_password = payload.get("admin_password", [""])[0].strip()
        if new_password:
            self.app_context.db.set_admin_password(new_password)
        self._send_redirect("/admin/imports")

    def _require_admin(self, handler, require_csrf: bool = False) -> None:
        if not self._is_admin_ip_allowed():
            self.send_error(HTTPStatus.FORBIDDEN, "Admin access denied")
            return
        if not self.app_context.sessions.validate(self._read_session_cookie()):
            self._send_redirect("/admin/login")
            return
        if require_csrf:
            if not self._verify_rate_limit(
                self.app_context.admin_post_limiter,
                f"admin-post:{self._client_ip()}",
                ADMIN_POST_RATE_LIMIT,
                ADMIN_POST_RATE_WINDOW_SECONDS,
                "后台操作过于频繁，请稍后再试",
            ):
                return
            payload = self._read_form()
            if payload is None:
                return
            csrf_token = payload.get("csrf_token", [""])[0]
            if not csrf_token or csrf_token != self._csrf_token_for_request():
                self.send_error(HTTPStatus.FORBIDDEN, "CSRF verification failed")
                return
        handler()

    def _read_session_cookie(self) -> str | None:
        cookie_header = self.headers.get("Cookie", "")
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        morsel = cookie.get("schedule_admin_session")
        return morsel.value if morsel else None

    def _read_form(self) -> dict[str, list[str]] | None:
        cached = getattr(self, "_cached_form_payload", None)
        if cached is not None:
            return cached
        body = self._read_body(MAX_FORM_BYTES)
        if body is None:
            return None
        payload = parse_qs(body.decode("utf-8"))
        self._cached_form_payload = payload
        return payload

    def _read_json_or_form(self) -> dict[str, object] | None:
        cached = getattr(self, "_cached_json_or_form_payload", None)
        if cached is not None:
            return cached
        content_type = self.headers.get("Content-Type", "")
        body = self._read_body(MAX_JSON_BYTES)
        if body is None:
            return None
        raw = body.decode("utf-8")
        if "application/json" in content_type:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return {}
            result = payload if isinstance(payload, dict) else {}
            self._cached_json_or_form_payload = result
            return result
        result = {key: values[0] for key, values in parse_qs(raw).items()}
        self._cached_json_or_form_payload = result
        return result

    def _send_html(self, title: str, body: str) -> None:
        content = _layout(title, body)
        encoded = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._send_security_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, payload, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _is_client_ip_allowed(client_ip: str, allowlist: str) -> bool:
    rules = [item.strip() for item in allowlist.split(",") if item.strip()]
    if not rules:
        return True
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for rule in rules:
        try:
            if address in ipaddress.ip_network(rule, strict=False):
                return True
            continue
        except ValueError:
            pass
        try:
            if address == ipaddress.ip_address(rule):
                return True
        except ValueError:
            continue
    return False


def _is_valid_ip_allowlist(allowlist: str) -> bool:
    for rule in [item.strip() for item in allowlist.split(",") if item.strip()]:
        try:
            ipaddress.ip_network(rule, strict=False)
            continue
        except ValueError:
            pass
        try:
            ipaddress.ip_address(rule)
        except ValueError:
            return False
    return True


def _weekday_options(selected: int) -> str:
    options = (
        (1, "星期一"),
        (2, "星期二"),
        (3, "星期三"),
        (4, "星期四"),
        (5, "星期五"),
        (6, "星期六"),
        (7, "星期日"),
    )
    return "".join(
        f"<option value='{value}' {'selected' if value == selected else ''}>{html.escape(label)}</option>"
        for value, label in options
    )


def _scope_options(selected: str) -> str:
    options = (
        ("all_day", "全天"),
        ("morning", "上午"),
        ("afternoon", "下午"),
        ("evening", "晚上"),
    )
    return "".join(
        f"<option value='{value}' {'selected' if value == selected else ''}>{html.escape(label)}</option>"
        for value, label in options
    )


def _render_weekday_radios(selected: int) -> str:
    options = (
        (1, "周一"),
        (2, "周二"),
        (3, "周三"),
        (4, "周四"),
        (5, "周五"),
        (6, "周六"),
        (7, "周日"),
    )
    return "".join(
        f"<label><input type='radio' name='weekday' value='{value}' {'checked' if value == selected else ''}>"
        f"<span class='weekday-pill'>{label}</span></label>"
        for value, label in options
    )


def _resolve_query_request(params: dict[str, list[str]], current_context: dict, semester_start: date) -> tuple[date | None, int, int, str]:
    raw_date = params.get("date", [""])[0].strip()
    requested_date = parse_date_input(raw_date)
    week = int(params.get("week", [current_context["week"]])[0] or current_context["week"])
    weekday = int(params.get("weekday", [current_context["weekday"]])[0] or current_context["weekday"])
    scope = params.get("scope", ["all_day"])[0] or "all_day"
    if requested_date:
        derived_week = date_to_week(semester_start, requested_date)
        derived_weekday = requested_date.isoweekday()
        if derived_week != week or derived_weekday != weekday:
            requested_date = week_day_to_date(semester_start, week, weekday)
        else:
            week, weekday = derived_week, derived_weekday
    else:
        requested_date = week_day_to_date(semester_start, week, weekday)
    return requested_date, week, weekday, scope


def _resolve_people_selection(params: dict[str, list[str]]) -> tuple[list[str], str]:
    people_mode = params.get("people_mode", ["all"])[0].strip() or "all"
    if people_mode not in {"all", "custom"}:
        people_mode = "all"
    selected_people: list[str] = []
    seen: set[str] = set()
    for raw_name in params.get("person", []):
        name = raw_name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        selected_people.append(name)
    return selected_people, people_mode


def _build_query_string(
    week: int,
    weekday: int,
    scope: str,
    query_date: str,
    selected_people: list[str],
    people_mode: str,
) -> str:
    params: list[tuple[str, str]] = [
        ("week", str(week)),
        ("weekday", str(weekday)),
        ("scope", scope),
        ("date", query_date),
    ]
    if people_mode == "custom":
        params.append(("people_mode", "custom"))
        params.extend(("person", name) for name in selected_people)
    return urlencode(params, doseq=True)


def _render_status_sections(summary: dict, scope: str) -> str:
    labels = _status_group_labels(scope)
    cards = []
    mapping = (
        ("all_free", "free", summary.get("groups", {}).get("all_free", [])),
        ("all_busy", "busy", summary.get("groups", {}).get("all_busy", [])),
        ("partial", "", summary.get("groups", {}).get("partial", [])),
    )
    for key, class_name, names in mapping:
        tags = "".join(
            f'<span class="tag {"partial" if key == "partial" else ""}">{html.escape(name)}</span>' for name in names
        ) or '<span class="tag">暂无</span>'
        cards.append(
            f'<article class="summary-card {class_name}"><h3>{html.escape(labels[key])}</h3><div class="people-tags">{tags}</div></article>'
        )
    return "".join(cards)


def _render_people_filter_panel(availability: dict) -> str:
    available_people = availability.get("available_people", [])
    people_mode = availability.get("people_mode", "all")
    selected_people = set(availability.get("selected_people", []))
    selected_count = len(available_people) if people_mode != "custom" else len(selected_people)
    all_free_people = availability.get("summary", {}).get("groups", {}).get("all_free", [])
    chips = []
    for name in available_people:
        checked = people_mode != "custom" or name in selected_people
        chips.append(
            "<label class='person-chip'>"
            f"<input data-person-checkbox form='structured-form' type='checkbox' name='person' value='{html.escape(name)}' {'checked' if checked else ''}>"
            f"<span>{html.escape(name)}</span>"
            "</label>"
        )
    return f"""
    <section class="panel">
      <div class="panel-head">
        <h2>人员筛选</h2>
        <span id="people-selection-count" class="section-hint">当前 {selected_count}/{len(available_people)} 人</span>
      </div>
      <div class="person-toolbar">
        <button type="button" class="ghost" id="select-all-people">全选</button>
        <button type="button" class="ghost" id="clear-people">清空</button>
        <button type="button" class="ghost" id="only-free-people" data-free-people='{html.escape(json.dumps(all_free_people, ensure_ascii=False))}' {'disabled' if not all_free_people else ''}>仅看整段空闲</button>
      </div>
      <div class="person-chip-grid">{''.join(chips) or '<span class="tag">暂无已导入人员</span>'}</div>
    </section>
    """


def _render_summary_strip(availability: dict) -> str:
    counts = availability.get("summary", {}).get("counts", {})
    groups = availability.get("summary", {}).get("groups", {})
    selected_count = counts.get("selected", 0)
    free_names = _join_names(groups.get("all_free", []))
    return (
        "<div class='summary-strip'>"
        f"<span class='summary-pill strong'>当前 {selected_count} 人</span>"
        f"<span class='summary-pill'>整段空闲 {counts.get('all_free', 0)}</span>"
        f"<span class='summary-pill'>部分可约 {counts.get('partial', 0)}</span>"
        f"<span class='summary-pill'>整段忙碌 {counts.get('all_busy', 0)}</span>"
        f"<span class='summary-pill wide'>当前整段空闲：{html.escape(free_names)}</span>"
        "</div>"
    )


def _render_collaboration_heatmap(availability: dict) -> str:
    collaboration = availability.get("collaboration", {})
    rows = collaboration.get("heatmap", [])
    if not rows or int(collaboration.get("total_people", 0)) == 0:
        return "<div class='empty-state'>先选择至少 1 个人，再看协同热力图。</div>"
    selected_people = availability.get("selected_people", [])
    people_mode = availability.get("people_mode", "all")
    headers = "".join(f"<th>{html.escape(item['weekday_label'])}</th>" for item in rows)
    scope_order = ("morning", "afternoon", "evening")
    body_rows = []
    for scope in scope_order:
        scope_cells = []
        for weekday_row in rows:
            item = next(entry for entry in weekday_row["items"] if entry["scope"] == scope)
            total_count = max(1, int(item["total_count"]))
            heat = item["free_count"] / total_count
            query = _build_query_string(
                week=int(item["week"]),
                weekday=int(item["weekday"]),
                scope=str(item["scope"]),
                query_date=str(item["date"]),
                selected_people=selected_people,
                people_mode=people_mode,
            )
            scope_cells.append(
                "<td>"
                f"<a class='heat-cell' style='--heat:{heat:.3f}' href='/?{query}'>"
                f"<strong>{item['free_count']}/{item['total_count']}</strong>"
                "<span>完全空</span>"
                f"<small>另 {item['partial_count']} 人有空档</small>"
                "</a>"
                "</td>"
            )
        body_rows.append(
            "<tr>"
            f"<th>{html.escape(scope_label(scope))}</th>"
            f"{''.join(scope_cells)}"
            "</tr>"
        )
    return (
        "<div class='heatmap-wrap'>"
        "<table class='heatmap-table'>"
        f"<thead><tr><th>时段</th>{headers}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table>"
        "</div>"
    )


def _render_collaboration_rankings(availability: dict) -> str:
    collaboration = availability.get("collaboration", {})
    rankings = collaboration.get("rankings", [])
    if not rankings or int(collaboration.get("total_people", 0)) == 0:
        return "<div class='empty-state'>先选择至少 1 个人，再看人最齐时段排行。</div>"
    selected_people = availability.get("selected_people", [])
    people_mode = availability.get("people_mode", "all")
    cards = []
    for index, item in enumerate(rankings, start=1):
        query = _build_query_string(
            week=int(item["week"]),
            weekday=int(item["weekday"]),
            scope=str(item["scope"]),
            query_date=str(item["date"]),
            selected_people=selected_people,
            people_mode=people_mode,
        )
        cards.append(
            f"<a class='ranking-card' href='/?{query}'>"
            f"<span class='ranking-index'>{index}</span>"
            "<div class='ranking-copy'>"
            f"<strong>{html.escape(item['weekday_label'])} · {html.escape(item['scope_label'])}</strong>"
            f"<p>{item['free_count']}/{item['total_count']} 人完全空，另 {item['partial_count']} 人有空档</p>"
            f"<span class='muted'>{html.escape(_summarize_names(item.get('free_people', [])))}</span>"
            "</div>"
            "</a>"
        )
    return "<div class='ranking-list'>" + "".join(cards) + "</div>"


def _summarize_names(values: list[str], limit: int = 4) -> str:
    if not values:
        return "当前没有整段空闲的人"
    if len(values) <= limit:
        return "整段空闲：" + "、".join(values)
    head = "、".join(values[:limit])
    return f"整段空闲：{head} 等 {len(values)} 人"


def _render_matrix_rows(availability: dict) -> str:
    people = availability["people"]
    periods = availability["periods"]
    period_details = {item["period"]: item for item in availability.get("period_details", [])}
    if not people:
        return ""
    rows = []
    for period in periods:
        period_detail = period_details.get(period, {"label": f"第{period}节", "time": ""})
        cells = []
        for person in people:
            slot = next((item for item in person["slots"] if item["period"] == period), None)
            if not slot or slot["status"] == "free":
                cells.append('<td class="slot-free"><span class="slot-label">空闲</span></td>')
                continue
            detail = {
                "course_name": slot["course_name"],
                "course_code": slot.get("course_code", ""),
                "teacher": slot.get("teacher", ""),
                "location": slot.get("location", ""),
                "weeks_text": slot.get("weeks_text", ""),
                "period_start": slot.get("period_start", period),
                "period_end": slot.get("period_end", period),
                "course_time_text": slot.get("course_time_text", ""),
            }
            detail_json = html.escape(json.dumps(detail, ensure_ascii=False))
            cells.append(
                "<td class='slot-busy'>"
                f"<button type='button' class='course-cell-btn' data-detail='{detail_json}'>"
                f"<strong>{html.escape(slot['course_name'])}</strong>"
                f"<span>{html.escape(slot.get('location', '') or '点击查看详情')}</span>"
                "</button></td>"
            )
        rows.append(
            "<tr>"
            f"<td class='matrix-period'><strong>{html.escape(period_detail['label'])}</strong>"
            f"<span>{html.escape(period_detail['time'] or '时间未配置')}</span></td>"
            f"{''.join(cells)}"
            "</tr>"
        )
    return "".join(rows)


def _build_natural_language_response(question: str, parsed: dict, availability: dict) -> dict:
    meta = availability["meta"]
    labels = _status_group_labels(meta["scope"])
    groups = availability["summary"].get("groups", {})
    answer = (
        f"{meta['date']} 第{meta['week']}周 {meta['weekday_label']} {meta['scope_label']}："
        f"{labels['all_free']} { _join_names(groups.get('all_free', [])) }；"
        f"{labels['all_busy']} { _join_names(groups.get('all_busy', [])) }；"
        f"{labels['partial']} { _join_names(groups.get('partial', [])) }。"
    )
    return {
        "question": question,
        "query_label": f"{meta['date']} / 第{meta['week']}周 / {meta['weekday_label']} / {meta['scope_label']}",
        "answer": answer,
        "parsed": {
            "date": parsed.get("date", meta["date"]),
            "week": meta["week"],
            "weekday": meta["weekday"],
            "scope": meta["scope"],
            "source": parsed.get("source", "local"),
        },
        "groups": {
            "all_free": {"label": labels["all_free"], "people": groups.get("all_free", [])},
            "all_busy": {"label": labels["all_busy"], "people": groups.get("all_busy", [])},
            "partial": {"label": labels["partial"], "people": groups.get("partial", [])},
        },
    }


def _status_group_labels(scope: str) -> dict[str, str]:
    if scope == "all_day":
        return {"all_free": "全天空闲", "all_busy": "全天忙碌", "partial": "有空闲时间"}
    scope_text = {
        "morning": "上午",
        "afternoon": "下午",
        "evening": "晚上",
    }.get(scope, "该时段")
    return {
        "all_free": f"{scope_text}空闲",
        "all_busy": f"{scope_text}忙碌",
        "partial": f"{scope_text}有空闲时间",
    }


def _join_names(values: list[str]) -> str:
    return "、".join(values) if values else "暂无"


def _layout(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #f6f0e5;
      --paper: rgba(255,255,255,0.88);
      --ink: #1e2a2f;
      --muted: #607076;
      --accent: #c75b39;
      --line: rgba(30,42,47,0.12);
      --free: #d7efe0;
      --busy: #f7ddd2;
      --shadow: 0 18px 60px rgba(54, 44, 36, 0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(199,91,57,0.18), transparent 26%),
        radial-gradient(circle at right 20%, rgba(52,124,110,0.18), transparent 24%),
        linear-gradient(135deg, #f8f1e4 0%, #efe6d7 100%);
    }}
    .shell {{
      width: min(1180px, calc(100vw - 28px));
      margin: 22px auto;
      padding: 20px;
      border: 1px solid rgba(255,255,255,0.5);
      border-radius: 24px;
      background: rgba(255,255,255,0.45);
      backdrop-filter: blur(18px);
      box-shadow: var(--shadow);
    }}
    h1, h2, h3, h4 {{ margin: 0; font-weight: 700; }}
    p {{ margin: 0; color: var(--muted); }}
    form {{ display: grid; gap: 12px; }}
    label {{
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 13px;
    }}
    input, select, textarea {{
      width: 100%;
      border: 1px solid rgba(30,42,47,0.14);
      border-radius: 14px;
      padding: 11px 12px;
      font: inherit;
      color: var(--ink);
      background: rgba(255,255,255,0.92);
    }}
    textarea {{ min-height: 88px; resize: vertical; }}
    button {{
      border: none;
      border-radius: 999px;
      padding: 11px 16px;
      font: inherit;
      font-weight: 700;
      color: white;
      background: linear-gradient(135deg, #c75b39, #a74524);
      cursor: pointer;
    }}
    .ghost {{
      color: var(--ink);
      background: rgba(255,255,255,0.78);
      border: 1px solid var(--line);
    }}
    .hero {{
      display: grid;
      gap: 16px;
      grid-template-columns: 1.3fr 0.7fr;
      align-items: start;
      margin-bottom: 18px;
    }}
    .panel {{
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 22px;
      padding: 20px;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.55);
    }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 5px 10px;
      border-radius: 999px;
      background: rgba(30,42,47,0.07);
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 12px;
    }}
    .hero-copy {{
      display: grid;
      gap: 14px;
    }}
    .headline {{
      font-size: clamp(30px, 4vw, 48px);
      line-height: 1.02;
      letter-spacing: -0.03em;
      margin: 0;
    }}
    .subtitle {{
      margin: 0;
      color: var(--muted);
      font-size: 15px;
    }}
    .hero-grid {{
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
    }}
    .metric {{
      padding: 14px;
      border-radius: 18px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.7);
      backdrop-filter: blur(8px);
    }}
    .metric strong {{
      display: block;
      font-size: 20px;
      margin-top: 6px;
      line-height: 1.2;
    }}
    .panel-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 12px;
    }}
    .panel-head h2 {{
      margin: 0;
    }}
    .section-hint {{
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }}
    .quick-links {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .people-strip {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 2px;
    }}
    .filters {{
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      margin-bottom: 14px;
    }}
    .filters .full-row {{ grid-column: 1 / -1; }}
    .weekday-pills {{
      display: grid;
      gap: 8px;
      grid-template-columns: repeat(7, minmax(0, 1fr));
    }}
    .weekday-pills input {{
      position: absolute;
      opacity: 0;
      pointer-events: none;
    }}
    .weekday-pill {{
      display: block;
      text-align: center;
      padding: 10px 8px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.88);
      cursor: pointer;
      user-select: none;
    }}
    .weekday-pills input:checked + .weekday-pill {{
      color: white;
      background: linear-gradient(135deg, #c75b39, #a74524);
      border-color: transparent;
    }}
    .actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    button[disabled] {{
      opacity: 0.5;
      cursor: not-allowed;
    }}
    .toolbar {{
      display: grid;
      gap: 18px;
      margin-bottom: 16px;
    }}
    .toolbar-head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
    }}
    .toolbar-actions {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    .toolbar-meta {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .mini-stat {{
      padding: 10px 14px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.72);
      color: var(--muted);
      font-size: 13px;
    }}
    .mini-stat strong {{
      color: var(--ink);
      margin-left: 6px;
    }}
    .compact-grid {{
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }}
    .shortcut-row {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .person-toolbar {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 14px;
    }}
    .person-chip-grid {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .person-chip {{
      position: relative;
    }}
    .person-chip input {{
      position: absolute;
      opacity: 0;
      pointer-events: none;
    }}
    .person-chip span {{
      display: inline-flex;
      align-items: center;
      padding: 10px 14px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.82);
      cursor: pointer;
      transition: background 140ms ease, transform 140ms ease, border-color 140ms ease;
    }}
    .person-chip input:checked + span {{
      color: white;
      background: linear-gradient(135deg, #1f564b, #2d7a6a);
      border-color: transparent;
      transform: translateY(-1px);
    }}
    .summary {{
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      margin: 16px 0;
    }}
    .summary-card {{
      border-radius: 18px;
      padding: 16px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.76);
    }}
    .summary-card.free {{ background: var(--free); }}
    .summary-card.busy {{ background: var(--busy); }}
    .summary-card h3 {{
      margin: 0;
    }}
    .people-tags {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }}
    .tag {{
      padding: 6px 10px;
      border-radius: 999px;
      background: rgba(255,255,255,0.72);
      border: 1px solid rgba(30,42,47,0.08);
      color: var(--ink);
      font-size: 13px;
    }}
    .tag.partial {{
      background: rgba(255, 238, 193, 0.92);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 14px;
      overflow: hidden;
      border-radius: 18px;
      background: rgba(255,255,255,0.82);
    }}
    th, td {{
      padding: 12px 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      font-size: 14px;
    }}
    th {{
      color: var(--muted);
      background: rgba(30,42,47,0.04);
    }}
    .slot-free {{ background: rgba(215,239,224,0.6); }}
    .slot-busy {{ background: rgba(247,221,210,0.62); }}
    .slot-mixed {{ background: rgba(255,238,193,0.75); }}
    .matrix-period {{
      min-width: 118px;
      font-weight: 700;
      background: rgba(30,42,47,0.04);
    }}
    .matrix-period strong {{
      display: block;
    }}
    .matrix-period span {{
      display: block;
      margin-top: 5px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 500;
    }}
    .slot-label {{
      display: inline-block;
      font-weight: 600;
    }}
    .slot-free, .slot-busy {{
      padding: 8px;
    }}
    .course-cell-btn {{
      width: 100%;
      text-align: left;
      color: inherit;
      background: rgba(255,255,255,0.55);
      border: 1px solid rgba(30,42,47,0.08);
      border-radius: 16px;
      padding: 12px;
      font: inherit;
      cursor: pointer;
      transition: transform 140ms ease, box-shadow 140ms ease, background 140ms ease;
    }}
    .course-cell-btn:hover {{
      transform: translateY(-1px);
      background: rgba(255,255,255,0.84);
      box-shadow: 0 12px 24px rgba(54, 44, 36, 0.08);
    }}
    .course-cell-btn strong {{
      display: block;
      line-height: 1.35;
    }}
    .course-cell-btn span {{
      display: block;
      font-size: 12px;
      color: var(--muted);
      margin-top: 4px;
    }}
    .summary-strip {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin: 10px 0 6px;
    }}
    .summary-pill {{
      display: inline-flex;
      align-items: center;
      padding: 10px 14px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.82);
      font-size: 13px;
      color: var(--ink);
    }}
    .summary-pill.strong {{
      background: rgba(30,42,47,0.9);
      border-color: transparent;
      color: white;
    }}
    .summary-pill.wide {{
      max-width: 100%;
    }}
    .insight-grid {{
      display: grid;
      gap: 16px;
      grid-template-columns: minmax(0, 1.25fr) minmax(280px, 0.75fr);
      align-items: start;
    }}
    .heatmap-wrap {{
      overflow-x: auto;
    }}
    .heatmap-table {{
      margin-top: 0;
      min-width: 820px;
    }}
    .heatmap-table th,
    .heatmap-table td {{
      text-align: center;
      vertical-align: middle;
    }}
    .heatmap-table th:first-child {{
      min-width: 72px;
    }}
    .heat-cell {{
      display: block;
      min-width: 110px;
      padding: 14px 10px;
      text-decoration: none;
      color: inherit;
      border-radius: 16px;
      border: 1px solid rgba(30,42,47,0.08);
      background:
        linear-gradient(180deg, rgba(255,255,255,0.9), rgba(199,91,57, calc(0.1 + var(--heat) * 0.22))),
        rgba(255,255,255,0.88);
      transition: transform 140ms ease, box-shadow 140ms ease;
    }}
    .heat-cell:hover {{
      transform: translateY(-1px);
      box-shadow: 0 12px 24px rgba(54, 44, 36, 0.08);
    }}
    .heat-cell strong,
    .heat-cell span,
    .heat-cell small {{
      display: block;
    }}
    .heat-cell strong {{
      font-size: 18px;
    }}
    .heat-cell span {{
      margin-top: 4px;
      font-size: 12px;
      color: var(--muted);
    }}
    .heat-cell small {{
      margin-top: 6px;
      color: var(--ink);
    }}
    .ranking-list {{
      display: grid;
      gap: 10px;
    }}
    .ranking-card {{
      display: grid;
      grid-template-columns: 42px 1fr;
      gap: 12px;
      align-items: start;
      padding: 14px;
      border-radius: 18px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.82);
      text-decoration: none;
      color: inherit;
      transition: transform 140ms ease, box-shadow 140ms ease;
    }}
    .ranking-card:hover {{
      transform: translateY(-1px);
      box-shadow: 0 12px 24px rgba(54, 44, 36, 0.08);
    }}
    .ranking-index {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 42px;
      height: 42px;
      border-radius: 50%;
      background: linear-gradient(135deg, #c75b39, #a74524);
      color: white;
      font-weight: 700;
    }}
    .ranking-copy {{
      display: grid;
      gap: 6px;
    }}
    .empty-state {{
      padding: 18px;
      border-radius: 18px;
      border: 1px dashed var(--line);
      background: rgba(255,255,255,0.65);
      color: var(--muted);
    }}
    .matrix-wrap {{
      overflow-x: auto;
    }}
    .nl-collapsible {{
      margin-top: 16px;
    }}
    .nl-collapsible summary {{
      cursor: pointer;
      font-weight: 700;
      color: var(--ink);
      list-style: none;
    }}
    .nl-collapsible summary::-webkit-details-marker {{
      display: none;
    }}
    .nl-collapsible[open] summary {{
      margin-bottom: 14px;
    }}
    .nl-result-panel {{
      display: grid;
      gap: 14px;
      margin-top: 12px;
    }}
    .nl-answer {{
      border-radius: 18px;
      padding: 16px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.82);
    }}
    .detail-modal {{
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 18px;
      background: rgba(30,42,47,0.42);
      z-index: 20;
    }}
    .detail-modal.open {{ display: flex; }}
    .detail-card {{
      width: min(720px, 100%);
      max-height: 85vh;
      overflow: auto;
      padding: 18px;
      border-radius: 22px;
      background: rgba(255,255,255,0.96);
      box-shadow: var(--shadow);
    }}
    .detail-grid {{
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      margin-top: 14px;
    }}
    .detail-block {{
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 12px;
      background: rgba(248,241,228,0.62);
    }}
    .detail-block strong {{
      display: block;
      margin-bottom: 8px;
    }}
    .admin-grid {{
      display: grid;
      gap: 16px;
      grid-template-columns: 360px 1fr;
    }}
    .import-card {{
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 14px;
      background: rgba(255,255,255,0.72);
      margin-bottom: 12px;
    }}
    .muted {{ color: var(--muted); }}
    .danger {{ color: #9d2d12; }}
    @media (max-width: 920px) {{
      .hero, .admin-grid {{ grid-template-columns: 1fr; }}
      .shell {{ width: min(100vw - 16px, 1180px); margin: 10px auto; padding: 12px; }}
      table {{ display: block; overflow-x: auto; }}
      .toolbar-head,
      .panel-head {{
        flex-direction: column;
        align-items: flex-start;
      }}
      .compact-grid,
      .insight-grid {{
        grid-template-columns: 1fr;
      }}
      .toolbar-actions {{
        justify-content: flex-start;
      }}
      .section-hint {{ white-space: normal; }}
    }}
  </style>
</head>
<body>
  <main class="shell">{body}</main>
</body>
</html>"""


def _render_home_page(config: dict[str, str], availability: dict, current_context: dict, people_names: list[str]) -> str:
    meta = availability["meta"]
    matrix_headers = "".join(
        f"<th>{html.escape(person['person_name'])}<br><span class='muted'>{html.escape(person['student_id'])}</span></th>"
        for person in availability["people"]
    )
    matrix_rows = _render_matrix_rows(availability)
    semester_start_date = date.fromisoformat(config.get("semester_start_date", "2026-03-02"))
    semester_start = html.escape(semester_start_date.isoformat())
    selected_people = availability.get("selected_people", [])
    people_mode = availability.get("people_mode", "all")
    query_string = _build_query_string(
        week=int(meta["week"]),
        weekday=int(meta["weekday"]),
        scope=str(meta["scope"]),
        query_date=str(meta["date"]),
        selected_people=selected_people,
        people_mode=people_mode,
    )
    today_date = date.fromisoformat(current_context["today"])
    tomorrow_date = today_date + timedelta(days=1)
    today_query = _build_query_string(
        week=max(1, date_to_week(semester_start_date, today_date)),
        weekday=today_date.isoweekday(),
        scope=str(meta["scope"]),
        query_date=today_date.isoformat(),
        selected_people=selected_people,
        people_mode=people_mode,
    )
    tomorrow_query = _build_query_string(
        week=max(1, date_to_week(semester_start_date, tomorrow_date)),
        weekday=tomorrow_date.isoweekday(),
        scope=str(meta["scope"]),
        query_date=tomorrow_date.isoformat(),
        selected_people=selected_people,
        people_mode=people_mode,
    )
    current_week_date = week_day_to_date(semester_start_date, max(1, int(current_context["week"])), int(meta["weekday"]))
    current_week_query = _build_query_string(
        week=max(1, int(current_context["week"])),
        weekday=int(meta["weekday"]),
        scope=str(meta["scope"]),
        query_date=current_week_date.isoformat(),
        selected_people=selected_people,
        people_mode=people_mode,
    )
    counts = availability.get("summary", {}).get("counts", {})
    loaded_people_count = len(availability.get("available_people", [])) or len(people_names)
    matrix_hint = f"{meta['date']} / 第{meta['week']}周 / {meta['weekday_label']} / {meta['scope_label']}"
    return f"""
    <section class="panel toolbar">
      <div class="toolbar-head">
        <div>
          <span class="eyebrow">课表空闲查询</span>
          <h1 class="headline">第{meta['week']}周 {html.escape(meta['weekday_label'])}</h1>
          <p class="subtitle">{html.escape(meta['date'])} · {html.escape(meta['scope_label'])} · 当前筛选 {counts.get('selected', 0)} / {loaded_people_count} 人</p>
        </div>
        <div class="toolbar-actions">
          <a href="/admin/imports" class="ghost" style="text-decoration:none; padding:11px 16px; border-radius:999px;">管理后台</a>
          <a href="/api/availability?{query_string}" class="ghost" style="text-decoration:none; padding:11px 16px; border-radius:999px;">JSON</a>
        </div>
      </div>
      <form id="structured-form" method="get" action="/" data-semester-start="{semester_start}">
        <input id="people-mode" type="hidden" name="people_mode" value="{html.escape(people_mode)}">
        <div class="compact-grid">
          <label>指定日期
            <input id="structured-date" type="date" name="date" value="{html.escape(meta['date'])}">
          </label>
          <label>周次
            <input id="structured-week" type="number" min="1" name="week" value="{meta['week']}">
          </label>
          <label>范围
            <select id="structured-scope" name="scope">{_scope_options(meta['scope'])}</select>
          </label>
        </div>
        <div class="filters">
          <div class="full-row">
            <span class="muted">星期</span>
            <div class="weekday-pills">{_render_weekday_radios(meta['weekday'])}</div>
          </div>
        </div>
        <div class="shortcut-row">
          <a href="/?{today_query}" class="ghost" style="text-decoration:none; padding:11px 16px; border-radius:999px;">今天</a>
          <a href="/?{tomorrow_query}" class="ghost" style="text-decoration:none; padding:11px 16px; border-radius:999px;">明天</a>
          <a href="/?{current_week_query}" class="ghost" style="text-decoration:none; padding:11px 16px; border-radius:999px;">本周</a>
          <button type="submit">查询空闲</button>
        </div>
      </form>
    </section>

    {_render_people_filter_panel(availability)}

    <section class="panel">
      <div class="panel-head">
        <h2>协同洞察</h2>
        <span class="section-hint">优先显示人最齐的时段</span>
      </div>
      <div class="insight-grid">
        <div>
          <h3 style="margin-bottom: 10px;">本周热力图</h3>
          {_render_collaboration_heatmap(availability)}
        </div>
        <div>
          <h3 style="margin-bottom: 10px;">人最齐时段 Top 5</h3>
          {_render_collaboration_rankings(availability)}
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>空闲矩阵</h2>
        <span class="section-hint">{html.escape(matrix_hint)}</span>
      </div>
      {_render_summary_strip(availability)}
      <div class="matrix-wrap">
        <table>
          <thead>
            <tr>
              <th class="matrix-period">节次</th>
              {matrix_headers}
            </tr>
          </thead>
          <tbody>
            {matrix_rows or "<tr><td colspan='99'>还没有已生效课表，请先到后台确认草稿版本。</td></tr>"}
          </tbody>
        </table>
      </div>
    </section>

    <details class="panel nl-collapsible">
      <summary>自然语言查询</summary>
      <form id="nl-form">
        <textarea name="question" placeholder="例如：4月2日下午谁有空"></textarea>
        <div class="actions">
          <button type="submit">解析并查询</button>
        </div>
      </form>
      <div id="nl-result" class="nl-result-panel"></div>
    </details>

    <div id="detail-modal" class="detail-modal" aria-hidden="true">
      <div class="detail-card">
        <div class="actions" style="justify-content:space-between; align-items:center;">
          <h3 id="detail-title">课程详情</h3>
          <button id="detail-close" class="ghost" type="button">关闭</button>
        </div>
        <div id="detail-content"></div>
      </div>
    </div>

    <script>
      const semesterStart = new Date('{config.get("semester_start_date", "2026-03-02")}T00:00:00');
      const dateInput = document.getElementById('structured-date');
      const weekInput = document.getElementById('structured-week');
      const weekdayInputs = Array.from(document.querySelectorAll('input[name=\"weekday\"]'));
      const peopleModeInput = document.getElementById('people-mode');
      const personInputs = Array.from(document.querySelectorAll('[data-person-checkbox]'));
      const peopleCount = document.getElementById('people-selection-count');
      const onlyFreeButton = document.getElementById('only-free-people');
      function pad(value) {{
        return String(value).padStart(2, '0');
      }}
      function toIso(dateObj) {{
        return `${{dateObj.getFullYear()}}-${{pad(dateObj.getMonth() + 1)}}-${{pad(dateObj.getDate())}}`;
      }}
      function deriveDate(week, weekday) {{
        const next = new Date(semesterStart);
        next.setDate(semesterStart.getDate() + ((Number(week) - 1) * 7) + Number(weekday) - 1);
        return next;
      }}
      function deriveWeekdayFromDate(value) {{
        const current = new Date(`${{value}}T00:00:00`);
        const diffDays = Math.round((current - semesterStart) / 86400000);
        const week = Math.floor(diffDays / 7) + 1;
        const weekday = current.getDay() === 0 ? 7 : current.getDay();
        return {{ week, weekday }};
      }}
      function syncDateFromWeekday() {{
        const checked = weekdayInputs.find((input) => input.checked);
        if (!weekInput.value || !checked) return;
        dateInput.value = toIso(deriveDate(weekInput.value, checked.value));
      }}
      function syncWeekdayFromDate() {{
        if (!dateInput.value) return;
        const result = deriveWeekdayFromDate(dateInput.value);
        weekInput.value = result.week;
        weekdayInputs.forEach((input) => {{
          input.checked = Number(input.value) === result.weekday;
        }});
      }}
      weekInput.addEventListener('input', syncDateFromWeekday);
      weekdayInputs.forEach((input) => input.addEventListener('change', syncDateFromWeekday));
      dateInput.addEventListener('change', syncWeekdayFromDate);

      function syncPeopleMode() {{
        const checkedCount = personInputs.filter((input) => input.checked).length;
        const totalCount = personInputs.length;
        if (checkedCount === totalCount) {{
          peopleModeInput.value = 'all';
        }} else {{
          peopleModeInput.value = 'custom';
        }}
        if (peopleCount) {{
          peopleCount.textContent = `当前 ${{checkedCount}}/${{totalCount}} 人`;
        }}
      }}
      personInputs.forEach((input) => input.addEventListener('change', syncPeopleMode));
      document.getElementById('select-all-people')?.addEventListener('click', () => {{
        personInputs.forEach((input) => {{ input.checked = true; }});
        syncPeopleMode();
      }});
      document.getElementById('clear-people')?.addEventListener('click', () => {{
        personInputs.forEach((input) => {{ input.checked = false; }});
        peopleModeInput.value = 'custom';
        syncPeopleMode();
      }});
      onlyFreeButton?.addEventListener('click', () => {{
        const freeNames = JSON.parse(onlyFreeButton.dataset.freePeople || '[]');
        const freeSet = new Set(freeNames);
        personInputs.forEach((input) => {{
          input.checked = freeSet.has(input.value);
        }});
        peopleModeInput.value = 'custom';
        syncPeopleMode();
      }});
      syncPeopleMode();

      const form = document.getElementById('nl-form');
      const output = document.getElementById('nl-result');
      form.addEventListener('submit', async (event) => {{
        event.preventDefault();
        const question = form.question.value.trim();
        const selectedPeople = personInputs.filter((input) => input.checked).map((input) => input.value);
        if (!question) {{
          output.innerHTML = '<div class="nl-answer">请输入查询内容</div>';
          return;
        }}
        output.innerHTML = '<div class="nl-answer">正在解析...</div>';
        const response = await fetch('/api/query/nl', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            question,
            selected_people: selectedPeople,
            people_mode: peopleModeInput.value
          }})
        }});
        const data = await response.json();
        output.innerHTML = renderNaturalResult(data);
      }});

      function escapeHtml(value) {{
        return String(value ?? '').replace(/[&<>\"']/g, (char) => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}}[char]));
      }}
      function renderTagList(values, className='') {{
        if (!values || values.length === 0) return '<span class=\"tag\">暂无</span>';
        return values.map((value) => `<span class=\"tag ${{className}}\">${{escapeHtml(value)}}</span>`).join('');
      }}
      function renderNaturalResult(data) {{
        if (data.error) return `<div class=\"nl-answer\">${{escapeHtml(data.error)}}</div>`;
        const groups = data.groups || {{}};
        return `
          <div class=\"nl-answer\"><strong>${{escapeHtml(data.answer || '已完成查询')}}</strong><div class=\"muted\" style=\"margin-top:8px;\">${{escapeHtml(data.query_label || '')}}</div></div>
          <section class=\"summary\">
            <article class=\"summary-card free\"><h3>${{escapeHtml(groups.all_free?.label || '空闲')}}</h3><div class=\"people-tags\">${{renderTagList(groups.all_free?.people || [])}}</div></article>
            <article class=\"summary-card busy\"><h3>${{escapeHtml(groups.all_busy?.label || '忙碌')}}</h3><div class=\"people-tags\">${{renderTagList(groups.all_busy?.people || [])}}</div></article>
            <article class=\"summary-card\"><h3>${{escapeHtml(groups.partial?.label || '有空闲时间')}}</h3><div class=\"people-tags\">${{renderTagList(groups.partial?.people || [], 'partial')}}</div></article>
          </section>
        `;
      }}

      const modal = document.getElementById('detail-modal');
      const modalTitle = document.getElementById('detail-title');
      const modalContent = document.getElementById('detail-content');
      function renderPeriodText(detail) {{
        if (Number(detail.period_start) === Number(detail.period_end)) {{
          return `第${{escapeHtml(detail.period_start)}}节`;
        }}
        return `第${{escapeHtml(detail.period_start)}}-${{escapeHtml(detail.period_end)}}节`;
      }}
      document.querySelectorAll('.course-cell-btn').forEach((button) => {{
        button.addEventListener('click', () => {{
          const detail = JSON.parse(button.dataset.detail);
          modalTitle.textContent = detail.course_name || '课程详情';
          modalContent.innerHTML = `
            <div class=\"detail-grid\">
              <div class=\"detail-block\"><strong>课程号</strong><div>${{escapeHtml(detail.course_code || '未解析')}}</div></div>
              <div class=\"detail-block\"><strong>开课周次</strong><div>${{escapeHtml(detail.weeks_text || '未解析')}}</div></div>
              <div class=\"detail-block\"><strong>授课老师</strong><div>${{escapeHtml(detail.teacher || '未提供')}}</div></div>
              <div class=\"detail-block\"><strong>上课场地</strong><div>${{escapeHtml(detail.location || '未提供')}}</div></div>
              <div class=\"detail-block\"><strong>节次</strong><div>${{renderPeriodText(detail)}}</div></div>
              <div class=\"detail-block\"><strong>上课时间</strong><div>${{escapeHtml(detail.course_time_text || '未配置')}}</div></div>
            </div>
          `;
          modal.classList.add('open');
          modal.setAttribute('aria-hidden', 'false');
        }});
      }});
      document.getElementById('detail-close').addEventListener('click', () => {{
        modal.classList.remove('open');
        modal.setAttribute('aria-hidden', 'true');
      }});
      modal.addEventListener('click', (event) => {{
        if (event.target === modal) {{
          modal.classList.remove('open');
          modal.setAttribute('aria-hidden', 'true');
        }}
      }});
    </script>
    """


def _render_login_page(error_message: str) -> str:
    error_html = f"<p class='danger'>{html.escape(error_message)}</p>" if error_message else ""
    return f"""
    <section class="panel" style="max-width:420px; margin: 80px auto;">
      <span class="eyebrow">管理员入口</span>
      <h1>登录后台</h1>
      <p style="margin-top: 8px;">建议通过环境变量 <code>SCHEDULE_ADMIN_PASSWORD</code> 设置管理员密码；新部署未设置时会自动生成随机密码并打印到控制台。</p>
      {error_html}
      <form method="post" action="/admin/login" style="margin-top: 16px;">
        <label>管理员密码
          <input name="password" type="password" autocomplete="current-password">
        </label>
        <div class="actions">
          <button type="submit">登录</button>
          <a href="/" class="ghost" style="text-decoration:none; padding:11px 16px; border-radius:999px;">返回查询页</a>
        </div>
      </form>
    </section>
    """


def _render_admin_page(config: dict[str, str], details: list[dict | None], csrf_token: str) -> str:
    masked_api_key = _mask_secret(config.get("llm_api_key", ""))
    csrf_input = f"<input type='hidden' name='csrf_token' value='{html.escape(csrf_token)}'>"
    cards = ""
    for detail in details:
        if not detail:
            continue
        import_row = detail["import"]
        warnings = detail["warnings"]
        warning_html = "".join(f"<li>{html.escape(item)}</li>" for item in warnings) or "<li>无</li>"
        file_list = "".join(
            f"<li>{html.escape(item['person_name'])} / {html.escape(item['source_file'])}</li>"
            for item in detail["files"]
        ) or "<li>无</li>"
        preview_groups = _render_meeting_groups(detail["meetings"])
        confirm_form = ""
        if import_row["status"] != "current":
            confirm_form = (
                f"<form method='post' action='/admin/imports/{import_row['id']}/confirm'>"
                f"{csrf_input}"
                f"<button type='submit'>确认该版本为当前生效课表</button></form>"
            )
        cards += f"""
        <article class="import-card">
          <div class="actions" style="justify-content:space-between; align-items:center;">
            <div>
              <h3>版本 #{import_row['id']} <span class="muted">[{html.escape(import_row['status'])}]</span></h3>
              <p style="margin-top: 6px;">创建时间：{html.escape(import_row['created_at'])}，课表人数：{import_row['source_count']}，警告数：{import_row['warning_count']}</p>
            </div>
            {confirm_form}
          </div>
          <div class="admin-grid" style="margin-top: 16px;">
            <div>
              <h4>导入文件</h4>
              <ul>{file_list}</ul>
              <h4 style="margin-top: 12px;">警告</h4>
              <ul>{warning_html}</ul>
            </div>
            <div>
              <h4>课程预览</h4>
              {preview_groups}
            </div>
          </div>
        </article>
        """
    return f"""
    <section class="hero">
      <article class="panel">
        <span class="eyebrow">管理员后台</span>
        <div class="headline">导入草稿、核对结构化课表，再切换成当前版本。</div>
        <p>自动监控目录：{html.escape(config.get('watch_dir', ''))}</p>
        <p style="margin-top: 8px;">安全模式：后台操作已启用会话过期、CSRF、防刷限流。匿名自然语言查询默认不走外部模型。</p>
        <div class="actions" style="margin-top: 16px;">
          <form method="post" action="/admin/rescan">{csrf_input}<button type="submit">立即重扫目录</button></form>
          <form method="post" action="/admin/logout">{csrf_input}<button class="ghost" type="submit">退出登录</button></form>
          <a href="/" class="ghost" style="text-decoration:none; padding:11px 16px; border-radius:999px;">返回查询页</a>
        </div>
      </article>
      <aside class="panel">
        <h2>系统配置</h2>
        <form method="post" action="/admin/config">
          {csrf_input}
          <label>学期起始周一
            <input type="date" name="semester_start_date" value="{html.escape(config.get('semester_start_date', '2026-03-02'))}">
          </label>
          <label>OpenAI 兼容 Base URL
            <input name="llm_base_url" value="{html.escape(config.get('llm_base_url', ''))}" placeholder="例如 https://open.bigmodel.cn/api/paas/v4/">
          </label>
          <label>API Key
            <input type="password" name="llm_api_key" value="" placeholder="{html.escape(masked_api_key or '未配置；输入后保存')}">
          </label>
          <label>聊天模型
            <input name="llm_model" value="{html.escape(config.get('llm_model', ''))}" placeholder="例如 glm-5">
          </label>
          <label>视觉模型名称
            <input name="vision_model" value="{html.escape(config.get('vision_model', ''))}" placeholder="例如 glm-ocr">
          </label>
          <label>PDF 解析工具类型
            <input name="pdf_parser_tool_type" value="{html.escape(config.get('pdf_parser_tool_type', 'expert'))}" placeholder="推荐 expert">
          </label>
          <label>管理员 IP 白名单
            <input name="admin_trusted_ips" value="{html.escape(config.get('admin_trusted_ips', ''))}" placeholder="例如 127.0.0.1,192.168.1.10,192.168.1.0/24">
          </label>
          <label style="display:flex; gap:10px; align-items:center; margin-top: 8px;">
            <input type="checkbox" name="public_nl_remote_fallback" value="1" {'checked' if config.get('public_nl_remote_fallback', '0') == '1' else ''}>
            <span>允许游客自然语言查询调用外部模型兜底</span>
          </label>
          <label>修改管理员密码
            <input type="password" name="admin_password" placeholder="留空表示不修改">
          </label>
          <p>推荐配置：自然语言使用 `glm-5`，PDF 课表解析推荐智谱文件解析 `expert`，OCR/版面兜底推荐 `glm-ocr`。如需更安全，后台仅放行固定 IP，且不要给游客开放外部模型兜底。</p>
          <div class="actions">
            <button type="submit">保存配置</button>
          </div>
        </form>
      </aside>
    </section>
    <section class="panel" style="margin-top: 16px;">
      <h2>最近导入版本</h2>
      <p style="margin-top: 6px;">导入采用“草稿 -> 管理员确认 -> 当前生效版本”的两阶段流程，避免识别结果直接上线。</p>
      <div style="margin-top: 16px;">{cards or "<p>还没有导入版本。</p>"}</div>
    </section>
    """


def _render_meeting_groups(meetings: list[dict]) -> str:
    grouped: dict[str, list[dict]] = {}
    for meeting in meetings:
        grouped.setdefault(meeting["person_name"], []).append(meeting)
    if not grouped:
        return "<p>无</p>"

    groups_html = []
    for index, person_name in enumerate(sorted(grouped)):
        rows = []
        person_meetings = sorted(
            grouped[person_name],
            key=lambda item: (item["weekday"], item["period_start"], item["course_name"]),
        )
        for meeting in person_meetings:
            week_text = ",".join(str(value) for value in meeting["weeks"][:12])
            if len(meeting["weeks"]) > 12:
                week_text += "..."
            rows.append(
                f"<tr><td>{html.escape(meeting['course_name'])}</td>"
                f"<td>{weekday_label(meeting['weekday'])}</td>"
                f"<td>{meeting['period_start']}-{meeting['period_end']}</td>"
                f"<td>{html.escape(week_text)}</td>"
                f"<td>{html.escape(meeting['location'])}</td></tr>"
            )
        groups_html.append(
            "<details {open_attr}>"
            "<summary><strong>{name}</strong> <span class='muted'>共 {count} 条课程</span></summary>"
            "<table><thead><tr><th>课程</th><th>星期</th><th>节次</th><th>周次</th><th>地点</th></tr></thead>"
            "<tbody>{rows}</tbody></table>"
            "</details>".format(
                open_attr="open" if index == 0 else "",
                name=html.escape(person_name),
                count=len(person_meetings),
                rows="".join(rows),
            )
        )
    return "".join(groups_html)


def _mask_secret(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * (len(secret) - 8)}{secret[-4:]}"
