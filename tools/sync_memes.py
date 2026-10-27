# -*- coding: utf-8 -*-
"""手动同步梗Wiki（插件设置里也有定时任务，这个用于立刻跑一次）。

用法：
    python tools/sync_memes.py --db <数据目录>/jargon.db
    python tools/sync_memes.py --db ... --full
    python tools/sync_memes.py --db ... --max-pages 2
"""
import argparse
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from store import JargonStore  # noqa: E402
from wikinow import sync  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True,
                    help="jargon.db 路径，例如 data/plugin_data/astrbot_plugin_slang/jargon.db")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--sleep", type=float, default=0.5)
    ap.add_argument("--max-pages", type=int, default=0)
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()

    store = JargonStore(args.db)
    res = sync(store, limit=args.limit, sleep=args.sleep,
               max_pages=args.max_pages, full=args.full)
    print(f"完成：{res['updated']} 条更新，库内 {res['total']} 条，共 {res['pages']} 页")


if __name__ == "__main__":
    main()
