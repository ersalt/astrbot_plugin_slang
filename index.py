# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""内存关键词索引。

注入不再每次全表读取：启动（或数据变更后）把已审词条的名称/别名装进内存，
按首字符分桶；匹配时只扫文本里的字符对应的桶，复杂度与词条总量基本无关。
"""

import re

_SPLIT = re.compile(r"[,，、;；\s]+")


def keys_of(name: str, keywords: str = "", aliases: str = "") -> list[str]:
    out = []
    for part in _SPLIT.split(f"{name},{keywords},{aliases}"):
        k = part.strip().lower()
        if len(k) >= 2 and k not in out:
            out.append(k)
    return out


class KeywordIndex:
    def __init__(self):
        self.buckets: dict[str, list[tuple[str, int]]] = {}
        self.entries: dict[int, dict] = {}
        self._seq = 0

    def build(self, rows_by_table: dict[str, list[dict]]):
        self.buckets.clear()
        self.entries.clear()
        self._seq = 0
        for table, rows in rows_by_table.items():
            source = "manual" if table == "manual" else "ai"
            for r in rows:
                keys = keys_of(r.get("name", ""), r.get("keywords", ""),
                               r.get("aliases", ""))
                if not keys:
                    continue
                eid = self._seq
                self._seq += 1
                self.entries[eid] = {
                    "name": r["name"], "scope": r["scope"] or "",
                    "meaning": r["meaning"], "count": r.get("count") or 1,
                    "table": table, "source": source, "keys": keys,
                }
                for k in keys:
                    self.buckets.setdefault(k[0], []).append((k, eid))

    def match(self, text: str, scope: str = "", limit: int = 3) -> list[dict]:
        if not text or not self.entries:
            return []
        low = text.lower()
        best: dict[int, int] = {}
        for i, ch in enumerate(low):
            for key, eid in self.buckets.get(ch, ()):
                if low.startswith(key, i):
                    if len(key) > best.get(eid, 0):
                        best[eid] = len(key)
        if not best:
            return []
        # 同名同群优先手动，且只保留一条
        grouped: dict[tuple, tuple[dict, int]] = {}
        for eid, klen in best.items():
            e = self.entries[eid]
            if e["scope"] not in ("", scope):
                continue
            gkey = (e["name"], e["scope"])
            cur = grouped.get(gkey)
            if cur is None:
                grouped[gkey] = (e, klen)
            else:
                ce, ck = cur
                if e["source"] == "manual" and ce["source"] != "manual":
                    grouped[gkey] = (e, klen)
                elif e["source"] == ce["source"] and klen > ck:
                    grouped[gkey] = (e, klen)
        hits = list(grouped.values())
        hits.sort(key=lambda x: (0 if x[0]["source"] == "manual" else 1,
                                 -x[1], -x[0]["count"]))
        return [e for e, _ in hits[:limit]]
