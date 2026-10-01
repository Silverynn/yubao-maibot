"""只读核对 Heart 日志与 SQLite 原始事件；不启动机器人、不修改或清理日志。"""
from collections import Counter
from pathlib import Path
import argparse
import json
import re
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from heart_shared.readable import render_event

HEADER = re.compile(r"^#(\d+) 时间：", re.MULTILINE)


def inspect(root, start=12147, end=12159):
    database = Path(root) / "events.sqlite3"
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        focus_day = db.execute("SELECT day FROM events WHERE seq=?", (start,)).fetchone()
        print("只读数据库检查：", db.execute("PRAGMA quick_check").fetchone()[0])
        exports = db.execute("""SELECT f.*, src.channel, src.user_id, s.title FROM session_files f
            LEFT JOIN session_sources src ON src.session=f.session
            LEFT JOIN sessions s ON s.session=f.session ORDER BY f.day,f.filename""").fetchall()
        managed = {row["filename"]: row for row in exports}
        missing_total = extra_total = duplicate_total = unmatched_total = formatting_total = cursor_total = 0
        files = list((Path(root) / "logs").rglob("*.txt"))
        for file in files:
            name = file.relative_to(Path(root) / "logs").as_posix()
            row = managed.get(name)
            if row is None:
                print("未登记的文本文件（需另行核实，不自动删除）：", name)
                unmatched_total += 1
                continue
            text = file.read_text(encoding="utf-8-sig")
            numbers = [int(value) for value in HEADER.findall(text)]
            events = db.execute("SELECT * FROM events WHERE day=? AND session=? ORDER BY seq",
                                (row["day"], row["session"])).fetchall()
            expected = [event["seq"] for event in events]
            rendered = "\n\n".join(render_event(event, json.loads(event["payload"])) for event in events) + "\n"
            if text != rendered:
                formatting_total += 1
                print("正文/模板差异（需核实是否旧版格式，不自动重写）：", name)
            cursor = db.execute("SELECT * FROM log_exports WHERE day=? AND session=?",
                                (row["day"], row["session"])).fetchone()
            if not cursor or cursor["filename"] != name or cursor["byte_size"] != file.stat().st_size or (
                    expected and cursor["last_seq"] != expected[-1]):
                cursor_total += 1
                print("导出游标或文件长度不一致：", name)
            missing = sorted(set(expected) - set(numbers))
            extra = sorted(set(numbers) - set(expected))
            repeated = [seq for seq, count in Counter(numbers).items() if count > 1]
            missing_total += len(missing)
            extra_total += len(extra)
            duplicate_total += len(repeated)
            if missing or extra or repeated or numbers != expected:
                print("记录顺序/数量异常：", name, "缺失", missing, "多出", extra, "重复", repeated)
            if name.startswith("Live2D/"):
                user = str(row["user_id"] or "")
                identity = "本次网页访问身份" if "vtuber_visit_" in user else "旧版固定访客身份"
                print(json.dumps({"file": name, "identity": identity, "records": len(numbers),
                    "first": numbers[0] if numbers else None, "last": numbers[-1] if numbers else None,
                    "matches_database": numbers == expected}, ensure_ascii=False))
                if focus_day and row["day"] == focus_day[0]:
                    kinds = db.execute("SELECT kind,COUNT(*) AS count,MIN(time) AS first,MAX(time) AS last "
                        "FROM events WHERE day=? AND session=? GROUP BY kind ORDER BY MIN(seq)",
                        (row["day"], row["session"])).fetchall()
                    print("该文件事件分类：", json.dumps([dict(item) for item in kinds], ensure_ascii=False))
        for row in exports:
            if not (Path(root) / "logs" / row["filename"]).is_file():
                print("登记文件不存在：", row["filename"])
                unmatched_total += 1
        print("全部文件核对：", json.dumps({"files": len(files), "missing": missing_total,
            "extra": extra_total, "duplicates": duplicate_total, "unregistered_or_absent": unmatched_total,
            "body_or_template_differences": formatting_total, "export_cursor_mismatches": cursor_total}, ensure_ascii=False))
        print("指定编号区间（不输出聊天正文）：")
        rows = {row["seq"]: row for row in db.execute("""SELECT e.seq,e.day,e.time,e.session,e.kind,
            s.title,src.channel FROM events e LEFT JOIN sessions s ON s.session=e.session
            LEFT JOIN session_sources src ON src.session=e.session WHERE e.seq BETWEEN ? AND ? ORDER BY e.seq""",
            (start, end))}
        export_map = {(row["day"], row["session"]): row["filename"] for row in exports}
        for seq in range(start, end + 1):
            row = rows.get(seq)
            if row is None:
                print(f"#{seq}：数据库没有此编号的事件（编号被去重插入消耗或历史清理；不是文件漏写证据）")
            else:
                print(f"#{seq}：{row['time']}｜{row['kind']}｜{row['channel'] or '其他'}｜"
                      + export_map.get((row["day"], row["session"]), "未登记文件"))
        print("不同会话文件后缀来自真实会话标识的短摘要；这里只检查，不重新生成任何会话标识。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / "data/heart_observation")
    parser.add_argument("--start", type=int, default=12147)
    parser.add_argument("--end", type=int, default=12159)
    args = parser.parse_args()
    inspect(args.root, args.start, args.end)
