from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path

from scheduler.importer import ImportManager
from scheduler.llm import NaturalLanguageParser
from scheduler.storage import Database
from scheduler.web import AppContext, create_server


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="飞牛OS 局域网课表空闲查询服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8123, help="监听端口，默认 8123")
    parser.add_argument("--pdf-dir", default=".", help="课表 PDF 目录，默认当前目录")
    parser.add_argument("--state-dir", default=".schedule_state", help="数据库和运行状态目录")
    parser.add_argument("--poll-interval", type=int, default=15, help="目录轮询秒数，默认 15")
    parser.add_argument("--scan-only", action="store_true", help="只执行一次导入扫描，不启动 Web 服务")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base_dir = Path(args.pdf_dir).resolve()
    state_dir = Path(args.state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)

    db = Database(state_dir / "schedule.db")
    existing_config = db.get_config()
    configured_admin_password = os.getenv("SCHEDULE_ADMIN_PASSWORD", "").strip()
    generated_admin_password = ""
    if not configured_admin_password:
        if existing_config.get("admin_password_hash"):
            configured_admin_password = "admin"
        else:
            generated_admin_password = secrets.token_urlsafe(12)
            configured_admin_password = generated_admin_password
    db.initialize(
        default_admin_password=configured_admin_password,
        default_semester_start=os.getenv("SEMESTER_START_DATE", "2026-03-02"),
        watch_dir=str(base_dir),
    )
    config = db.get_config()
    parser = NaturalLanguageParser(
        base_url=config.get("llm_base_url", ""),
        api_key=config.get("llm_api_key", ""),
        model=config.get("llm_model", ""),
    )
    importer = ImportManager(db=db, pdf_dir=base_dir, poll_interval=args.poll_interval)
    result = importer.scan_once()
    if args.scan_only:
        print(result)
        return

    importer.start()
    server = create_server(args.host, args.port, AppContext(db, importer, parser))
    print(f"课表服务已启动: http://{args.host}:{args.port}")
    print(f"PDF 目录: {base_dir}")
    print(f"状态目录: {state_dir}")
    if generated_admin_password:
        print(f"首次管理员密码: {generated_admin_password}")
    elif os.getenv("SCHEDULE_ADMIN_PASSWORD") is None:
        print("管理员密码沿用已存在配置；如需加固，请设置环境变量 SCHEDULE_ADMIN_PASSWORD 或在后台修改。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        importer.stop()
        server.server_close()


if __name__ == "__main__":
    main()
