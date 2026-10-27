# -*- coding: utf-8 -*-
"""数据库维护：回收空闲页、更新统计信息。

用法：
    python tools/db_maintain.py --db <数据目录>/jargon.db

建议停掉 AstrBot 再跑（VACUUM 需要独占锁）。
"""
import argparse
import pathlib
import sqlite3


def report(tag: str, path: pathlib.Path):
    size = path.stat().st_size / 1024 / 1024
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    pages = c.execute("pragma page_count").fetchone()[0]
    free = c.execute("pragma freelist_count").fetchone()[0]
    c.close()
    print(f"{tag}: {size:.2f} MB, {pages} 页, 空闲 {free} 页")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    args = ap.parse_args()
    path = pathlib.Path(args.db)
    report("before", path)
    c = sqlite3.connect(path)
    try:
        c.execute("PRAGMA optimize")
        c.execute("VACUUM")
        c.execute("ANALYZE")
        print("VACUUM + ANALYZE 完成")
    finally:
        c.close()
    report("after", path)


if __name__ == "__main__":
    main()
