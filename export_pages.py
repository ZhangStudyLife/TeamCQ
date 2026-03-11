from __future__ import annotations

import argparse
import json
from pathlib import Path

from scheduler.storage import Database


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导出 GitHub Pages 静态课表数据")
    parser.add_argument("--state-dir", default=".schedule_state", help="运行状态目录，默认 .schedule_state")
    parser.add_argument("--out-dir", default="docs/data", help="导出目录，默认 docs/data")
    parser.add_argument("--filename", default="schedule-data.json", help="导出文件名，默认 schedule-data.json")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    state_dir = Path(args.state_dir).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    db = Database(state_dir / "schedule.db")
    payload = db.export_static_dataset()
    out_path = out_dir / args.filename
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已导出 GitHub Pages 数据: {out_path}")


if __name__ == "__main__":
    main()
