# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""梗Wiki 公开 API 同步（供插件定时调用，也供 tools/sync_memes.py 手动跑）。

接口文档 https://cnmeme.wiki/developers/api
- 无需鉴权，只读公开词条
- limit 最大 30，按更新时间倒序
- 文档要求：限并发、别密集轮询、429 遵守 Retry-After、保留来源
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

BASE = "https://cnmeme.wiki/api/v1/public/memes"
UA = "astrbot-plugin-slang/1.0 (+https://github.com/ersalt/astrbot_plugin_slang)"


def _parse_ts(raw) -> float:
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _fetch(url: str, timeout: int = 15, retries: int = 2) -> dict | None:
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries:
                wait = int(exc.headers.get("Retry-After") or 10)
                time.sleep(wait)
                continue
            return None
        except Exception:  # noqa: BLE001
            time.sleep(1 + attempt)
    return None


def sync(store, limit: int = 30, sleep: float = 0.5, max_pages: int = 0,
         full: bool = False, log=print) -> dict:
    """同步到 store.wiki 表，返回统计。增量靠 meta.wiki_synced_at。"""
    limit = max(1, min(30, int(limit)))
    watermark = 0.0 if full else float(store.meta_get("wiki_synced_at") or 0)
    if full:
        store.wiki_clear()

    offset, pages, total, newest = 0, 0, 0, watermark
    while True:
        url = f"{BASE}?{urllib.parse.urlencode({'limit': limit, 'offset': offset})}"
        data = _fetch(url)
        if not data:
            log(f"请求失败，停止于 offset={offset}")
            break
        items = data.get("items") or []
        if not items:
            break

        rows = []
        for it in items:
            title = (it.get("title") or "").strip()
            if not (2 <= len(title) <= 24):
                continue
            ts = _parse_ts(it.get("updated_at"))
            if watermark and ts and ts <= watermark:
                continue
            summary = " ".join((it.get("summary") or "").split())
            if not summary:
                continue
            rows.append({
                "title": title,
                "tags": "、".join(it.get("tags") or []),
                "summary": summary[:200],
                "updated_at": ts or time.time(),
            })
            newest = max(newest, ts)

        if rows:
            store.wiki_upsert_many(rows)
            total += len(rows)

        pages += 1
        offset += limit
        if pages % 20 == 0:
            log(f"已同步 {pages} 页，累计 {total} 条")

        if not data.get("has_more"):
            break
        if max_pages and pages >= max_pages:
            break
        time.sleep(sleep)

    store.meta_set("wiki_synced_at", str(newest))
    return {"pages": pages, "updated": total, "total": store.wiki_count(),
            "watermark": newest}
