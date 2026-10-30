# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""关键词命中与注入格式化。纯函数，无外部依赖。"""

import re

HEADER = "[群词条参考·回答相关问题时以此为准；与常识冲突时以这里为准，不要逐字复述]"
WIKI_HEADER = "[梗参考·最多接一句，不要科普]"

_SPLIT = re.compile(r"[,，、;；\s]+")


def _keys(entry: dict) -> list[str]:
    raw = f"{entry.get('name', '')},{entry.get('aliases', '')}"
    out = []
    for part in _SPLIT.split(raw):
        k = part.strip()
        if len(k) >= 2 and k not in out:
            out.append(k)
    return out


def _best_hit(cands: list[str], low: str) -> str:
    best = ""
    for k in cands:
        if len(k) >= 2 and k.lower() in low and len(k) > len(best):
            best = k
    return best


def match(entries: list[dict], text: str, limit: int = 3) -> list[dict]:
    """黑话命中，按 人工 > 自审 > 命中词长 > 次数 排序。"""
    if not text:
        return []
    low = text.lower()
    hits = []
    for e in entries:
        best = _best_hit(_keys(e), low)
        if best:
            item = dict(e)
            item["_hit"] = best
            hits.append(item)
    hits.sort(key=lambda e: (
        int(e.get("review_manual") or 0),
        int(e.get("review_auto") or 0),
        len(e["_hit"]),
        int(e.get("count") or 0),
    ), reverse=True)
    return hits[:limit]


def match_wiki(keys: list[tuple[str, str]], text: str, limit: int = 1) -> list[str]:
    """梗库命中：只认标题，不认 tags。

    tags 里全是「文字梗」「表情包」这类分类词，拿去做子串匹配会疯狂误命中
    （例如 tags 含「天天」，于是「今天天气不错」就会命中一条无关词条）。
    """
    if not text:
        return []
    low = text.lower()
    hits = []
    for title, _tags in keys:
        if len(title) >= 2 and title.lower() in low:
            hits.append((title, len(title)))
    hits.sort(key=lambda x: -x[1])
    return [t for t, _ in hits[:limit]]


def format_hint(hits: list[dict], max_chars: int = 200, budget: int = 700) -> str:
    if not hits:
        return ""
    lines = []
    used = len(HEADER)
    for e in hits:
        meaning = (e.get("meaning") or "").strip().replace("\n", " ")
        if len(meaning) > max_chars:
            meaning = meaning[:max_chars].rstrip("，。；; ") + "…"
        tag = ""
        if e.get("review_manual"):
            tag = "·手动"
        elif e.get("scope"):
            tag = "·群内"
        line = f"· {e.get('name')}：{meaning}{tag}"
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
    return (HEADER + "\n" + "\n".join(lines)) if lines else ""


def format_wiki(rec: dict, max_summary: int = 90, max_usage: int = 60) -> str:
    if not rec:
        return ""
    summary = " ".join((rec.get("summary") or "").split())
    usage = " ".join((rec.get("usage") or "").split())
    if len(summary) > max_summary:
        summary = summary[:max_summary].rstrip("，。；; ") + "…"
    if len(usage) > max_usage:
        usage = usage[:max_usage].rstrip("，。；; ") + "…"
    if not summary and not usage:
        return ""
    line = f"· {rec.get('title')}：{summary or usage}"
    if usage and summary:
        line += f"（用法：{usage}）"
    return WIKI_HEADER + "\n" + line + "（梗Wiki）"
