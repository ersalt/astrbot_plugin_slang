# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""业务层：审核方向、冲突策略、注入索引、分页合并。

API 层（main.py）只负责解析参数，所有规则集中在这里。
"""

import re

try:
    from .index import KeywordIndex
    from .store import APPROVED, PENDING, JargonStore, TABLES
except ImportError:  # 直接以脚本方式跑测试
    from index import KeywordIndex
    from store import APPROVED, PENDING, JargonStore, TABLES

SIDE = {
    "manual": "手动", "auto": "黑话库",
    "manual_pending": "手动待审", "auto_pending": "待审",
}


class SlangError(Exception):
    """可以直接展示给用户的业务错误。"""


def source_of(table: str) -> str:
    return "manual" if table in ("manual", "manual_pending") else "ai"


def target_of(table: str) -> str:
    return {
        "manual_pending": "manual", "auto_pending": "auto",
        "manual": "manual_pending", "auto": "auto_pending",
    }[table]


def public(row: dict, table: str) -> dict:
    return {
        "table": table,
        "source": source_of(table),
        "pending": table in PENDING,
        "id": f"{table}:{row['id']}",
        "content": row["name"],
        "meaning": row["meaning"],
        "count": row["count"],
        "scope": row["scope"] or "",
        "is_global": not row["scope"],
        "aliases": row.get("aliases", ""),
        "keywords": row.get("keywords", ""),
        "extra": row.get("extra", ""),
        "auto_note": row["auto_note"],
        "contributor": row["contributor"],
        "evidence": row["evidence"],
        "updated_at": row["updated_at"],
    }


class SlangService:
    def __init__(self, store: JargonStore):
        self.store = store
        self.index = KeywordIndex()
        self._dirty = True

    # ---------- 索引 ----------

    def _rebuild(self):
        self.index.build({t: self._repo(t).all() for t in APPROVED})
        self._dirty = False

    def match(self, text: str, scope: str = "", limit: int = 3) -> list[dict]:
        if self._dirty:
            self._rebuild()
        return self.index.match(text, scope, limit)

    def invalidate(self):
        self._dirty = True

    def _repo(self, table: str):
        if table not in TABLES:
            raise SlangError("未知的表（页面可能还是旧版本），请 Ctrl+F5 刷新后重试")
        return self.store.repos[table]

    # ---------- 写入 ----------

    @staticmethod
    def _norm_keywords(raw) -> str:
        parts = []
        for x in re.split(r"[,，、;；\s]+", str(raw or "")):
            x = x.strip()
            if x and x not in parts:
                parts.append(x)
        return ",".join(parts)[:300]

    def add_manual(self, name: str, meaning: str, scope: str = "",
                   keywords: str = "", extra: str = "", aliases: str = "") -> dict:
        name = (name or "").strip()[:32]
        meaning = (meaning or "").strip()[:1000]
        if not name or not meaning:
            raise SlangError("词条名和内容都不能为空")
        keywords = self._norm_keywords(keywords or aliases)
        for t in ("manual", "manual_pending"):
            if self._repo(t).find(name, scope):
                raise SlangError(
                    f"「{name}」在该作用域已存在（{SIDE[t]}），未覆盖。要改内容请用「修改」。")
        rec = self._repo("manual").insert({
            "name": name, "meaning": meaning, "scope": scope,
            "aliases": "", "keywords": keywords,
            "extra": (extra or "").strip()[:1000],
            "evidence": '["手动录入"]', "contributor": "手动录入",
        })
        self.invalidate()
        return rec

    def _bump(self, repo, rec: dict, evidence: str, auto_note: str,
              keywords: str = ""):
        merged = self._norm_keywords(
            ",".join(x for x in (rec.get("keywords") or "", keywords) if x))
        repo.update(rec["id"],
                    count=(rec["count"] or 1) + 1,
                    keywords=merged or rec.get("keywords") or "",
                    evidence=self.store.merge_evidence(
                        rec["evidence"], self.store.pack_evidence(evidence)),
                    auto_note=auto_note or rec["auto_note"])

    def submit(self, name: str, meaning: str, scope: str = "", aliases: str = "",
               evidence: str = "", auto_note: str = "", approved: bool = False,
               contributor: str = "", keywords: str = "") -> dict:
        name = (name or "").strip()[:32]
        meaning = (meaning or "").strip()[:1000]
        if not name or not meaning:
            return {}
        auto = self._repo("auto")
        pending = self._repo("auto_pending")
        kw = self._norm_keywords(keywords or aliases)
        done = auto.find(name, scope)
        if done:
            # 已审优先：重复学习只累加，绝不再造一条同名待审
            self._bump(auto, done, evidence, auto_note, kw)
        elif approved:
            draft = pending.find(name, scope)
            if draft:
                auto.insert({
                    "name": draft["name"], "aliases": draft["aliases"],
                    "keywords": draft.get("keywords", ""), "extra": draft.get("extra", ""),
                    "meaning": draft["meaning"], "scope": draft["scope"],
                    "count": draft["count"], "auto_note": draft["auto_note"],
                    "evidence": draft["evidence"], "contributor": draft["contributor"],
                    "created_at": draft["created_at"],
                })
                pending.delete(draft["id"])
            else:
                auto.insert({
                    "name": name, "meaning": meaning, "scope": scope,
                    "aliases": "", "keywords": kw,
                    "evidence": self.store.pack_evidence(evidence),
                    "auto_note": auto_note, "contributor": contributor,
                })
        else:
            draft = pending.find(name, scope)
            if draft:
                self._bump(pending, draft, evidence, auto_note, kw)
            else:
                pending.insert({
                    "name": name, "meaning": meaning, "scope": scope,
                    "aliases": "", "keywords": kw,
                    "evidence": self.store.pack_evidence(evidence),
                    "auto_note": auto_note, "contributor": contributor,
                })
        self.invalidate()
        return (auto.find(name, scope) or pending.find(name, scope) or {})

    def update(self, table: str, old_name: str, old_scope: str,
               name: str = "", meaning: str = "", scope: str | None = None,
               keywords: str | None = None, extra: str | None = None) -> dict:
        repo = self._repo(table)
        rec = repo.find(old_name, old_scope)
        if not rec:
            raise SlangError(f"没找到「{old_name}」")
        new_name = (name or old_name).strip()[:32]
        new_scope = rec["scope"] if scope is None else scope
        if new_name != old_name or new_scope != old_scope:
            clash = repo.find(new_name, new_scope)
            if clash and clash["id"] != rec["id"]:
                where = "全局" if not new_scope else f"群 {new_scope}"
                raise SlangError(
                    f"改不了：{where}已有同名条目「{new_name}」"
                    f"（{(clash['meaning'] or '')[:20]}…），请先处理那条")
        out = repo.update(rec["id"],
                          name=new_name,
                          meaning=(meaning or rec["meaning"]).strip()[:1000],
                          scope=new_scope,
                          keywords=(self._norm_keywords(keywords)
                                    if keywords is not None else None),
                          extra=(extra.strip()[:1000] if extra is not None else None))
        if not out:
            raise SlangError("改名或改归属失败，可能重名")
        self.invalidate()
        return out

    def delete(self, table: str, name: str, scope: str = "") -> None:
        repo = self._repo(table)
        rec = repo.find(name, scope)
        if not rec:
            raise SlangError(f"没找到「{name}」")
        repo.delete(rec["id"])
        self.invalidate()

    # ---------- 审核 ----------

    def approve(self, table: str, name: str, scope: str = "") -> str:
        if table not in TABLES:
            raise SlangError("未知的表")
        src = self._repo(table).find(name, scope)
        if not src:
            raise SlangError(f"没找到「{name}」")
        tgt = target_of(table)
        clash = self._repo(tgt).find(name, scope)
        if clash:
            raise SlangError(
                f"操作失败：{SIDE[tgt]}已有同名同群条目"
                f"（{(clash['meaning'] or '')[:20]}…），两条都保留，请先处理那条")
        self._repo(tgt).insert({
            "name": src["name"], "aliases": src["aliases"],
            "meaning": src["meaning"], "scope": src["scope"],
            "count": src["count"], "auto_note": src["auto_note"],
            "evidence": src["evidence"], "contributor": src["contributor"],
            "created_at": src["created_at"],
        })
        self._repo(table).delete(src["id"])
        self.invalidate()
        return "approved" if table in PENDING else "pending"

    def bulk(self, action: str, items: list[dict]) -> dict:
        approved = deleted = skipped = 0
        for it in items:
            table = str(it.get("table") or "")
            name = str(it.get("name") or "")
            scope = str(it.get("scope") or "")
            if table not in TABLES or not name:
                skipped += 1
                continue
            try:
                if action == "approve":
                    self.approve(table, name, scope)
                    approved += 1
                elif action == "delete":
                    self.delete(table, name, scope)
                    deleted += 1
                else:
                    skipped += 1
            except SlangError:
                skipped += 1
        return {"approved": approved, "deleted": deleted, "skipped": skipped}

    # ---------- 查询 ----------

    def list_entries(self, bucket: str, search: str = "", scope: str = "",
                     scoped: bool = False, page: int = 1, size: int = 50,
                     sort: str = "recent"):
        scope_arg = scope if scoped else None
        if bucket in ("manual", "auto"):
            total, rows = self._repo(bucket).list_page(
                search=search, scope=scope_arg, page=page, size=size, sort=sort)
            return total, [public(r, bucket) for r in rows]
        if bucket == "pending":
            rows = []
            for t in PENDING:
                for r in self._repo(t).all():
                    if scope_arg is not None and (r["scope"] or "") != scope_arg:
                        continue
                    if search and search not in r["name"] and search not in r["meaning"]:
                        continue
                    rows.append(public(r, t))
            if sort == "count":
                rows.sort(key=lambda x: (-x["count"], -x["updated_at"]))
            else:
                rows.sort(key=lambda x: -x["updated_at"])
            total = len(rows)
            return total, rows[(page - 1) * size: page * size]
        return 0, []

    def pending_scopes(self) -> list[dict]:
        merged: dict[str, int] = {}
        for t in PENDING:
            for s in self._repo(t).scopes():
                merged[s["scope"]] = merged.get(s["scope"], 0) + s["count"]
        return [{"scope": k, "count": v} for k, v in merged.items()]

    def tree_counts(self):
        manual = self._repo("manual").count()
        auto = self._repo("auto").count()
        pending = sum(self._repo(t).count() for t in PENDING)
        return manual, auto, pending

    def scopes_of(self, table: str) -> list[dict]:
        return self._repo(table).scopes()

    def stats(self) -> dict:
        manual, auto, pending = self.tree_counts()
        return {"total": manual + auto, "manual": manual, "auto": auto,
                "pending": pending}

    # ---------- 指令辅助 ----------

    def find_any(self, name: str) -> tuple[str, dict] | None:
        best = None
        for t in TABLES:
            for r in self._repo(t).all():
                if r["name"] == name and (best is None or r["updated_at"] > best[1]["updated_at"]):
                    best = (t, r)
        return best

    def all_approved(self, scope: str | None = None) -> list[dict]:
        out = []
        for t in APPROVED:
            for r in self._repo(t).all(scope):
                out.append(public(r, t))
        return out
