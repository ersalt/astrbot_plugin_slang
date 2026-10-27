# -*- coding: utf-8 -*-
"""关键词命中与注入格式化。纯函数，无外部依赖。"""

SEP = ",，、;；"

HEADER = "[群黑话参考·理解用，不要照念]"


def _keys(entry: dict) -> list[str]:
    raw = f"{entry.get('name', '')},{entry.get('aliases', '')}"
    out = []
    for part in raw.replace("，", ",").replace("、", ",").replace(";", ",").replace("；", ",").split(","):
        k = part.strip()
        if len(k) >= 2 and k not in out:
            out.append(k)
    return out


def match(entries: list[dict], text: str, limit: int = 3) -> list[dict]:
    """返回命中的条目（带 _hit 字段记录命中的关键词）。"""
    if not text:
        return []
    low = text.lower()
    hits = []
    for e in entries:
        best = ""
        for k in _keys(e):
            if k.lower() in low and len(k) > len(best):
                best = k
        if best:
            item = dict(e)
            item["_hit"] = best
            hits.append(item)

    hits.sort(key=lambda e: (
        int(e.get("manual_review") or 0),
        int(e.get("auto_review") or 0),
        len(e["_hit"]),
        int(e.get("count") or 0),
    ), reverse=True)
    return hits[:limit]


def format_hint(hits: list[dict], max_chars: int = 200, budget: int = 700) -> str:
    """拼成一段注入文本；超预算从后往前丢条目。"""
    if not hits:
        return ""
    lines = []
    used = len(HEADER)
    for e in hits:
        meaning = (e.get("meaning") or "").strip().replace("\n", " ")
        if len(meaning) > max_chars:
            meaning = meaning[:max_chars].rstrip("，。；; ") + "…"
        tag = ""
        if e.get("manual_review"):
            tag = "·手动"
        elif e.get("category") == "group":
            tag = "·群内"
        line = f"· {e.get('name')}：{meaning}{tag}"
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return HEADER + "\n" + "\n".join(lines)
