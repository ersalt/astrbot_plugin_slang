# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""群语汇：群黑话学习与注入。

设计要点：
- 消息缓冲落 SQLite，重启不丢，不靠内存计数
- 提取后由模型自审：有把握进黑话库，没把握进待审
- 注入走关键词命中，最多 inject_max 条，纯机械匹配，不调模型
- 注入内容作为临时 user content 追加，不改 system prompt
"""

import asyncio
import json
import pathlib
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:  # 旧版 AstrBot 无 web 模块
    from astrbot.api.web import request as web_request
except Exception:  # noqa: BLE001
    web_request = None

try:  # 临时上下文用，旧版没有则该功能降级
    from astrbot.core.agent.message import TextPart
except Exception:  # noqa: BLE001
    TextPart = None

from .matcher import format_hint, format_wiki, match, match_wiki
from .miner import Miner
from .store import JargonStore


def _read_version(default: str = "0.0.0") -> str:
    """版本号只维护 metadata.yaml 一处，避免两处不一致。"""
    try:
        text = pathlib.Path(__file__).with_name("metadata.yaml").read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return default
    for line in text.splitlines():
        if line.strip().startswith("version:"):
            value = line.split(":", 1)[1].strip().strip("\"'")
            if value:
                return value
    return default


PLUGIN_VERSION = _read_version()


@register(
    "astrbot_plugin_slang",
    "贰叔",
    "群黑话自动学习与注入：LLM 提取 + 模型自审 + 待审复核，按群隔离，SQLite 持久化",
    PLUGIN_VERSION,
    "",
)
class JargonLite(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}

        self.enabled = bool(self.config.get("enabled", True))
        self.auto_learn = bool(self.config.get("auto_learn", True))
        self.learn_frequency = max(5, int(self.config.get("learn_frequency", 50)))
        self.inject_max = max(1, int(self.config.get("inject_max", 3)))
        self.max_meaning = max(20, int(self.config.get("max_meaning_chars", 200)))
        self.wiki_enable = bool(self.config.get("wiki_enable", True))
        self.listen_private = bool(self.config.get("listen_private", False))
        self.blacklist = self._parse_list(self.config.get("blacklist_groups", ""))

        data_dir = StarTools.get_data_dir("astrbot_plugin_slang")
        data_dir.mkdir(parents=True, exist_ok=True)
        self.store = JargonStore(str(data_dir / "jargon.db"))
        self.miner = Miner(context, self.store, self.config)
        try:
            self._wiki_keys = self.store.wiki_keys()
        except Exception:  # noqa: BLE001
            self._wiki_keys = []
        self._locks: dict[str, asyncio.Lock] = {}
        self._learning: set[str] = set()
        self._wiki_task = None
        self._wiki_running = False
        self._web_registered = False
        self._register_web_api()

    async def initialize(self):
        try:
            self._wiki_keys = self.store.wiki_keys()
        except Exception:  # noqa: BLE001
            self._wiki_keys = []
        s = self.store.stats()
        logger.info(
            f"[slang] 已加载：黑话 {s['total']} 条"
            f"（手动 {s['manual']}，自动 {s['auto']}），待审 {s['pending']}"
            f"，梗库 {len(self._wiki_keys)} 条")
        self._wiki_task = asyncio.create_task(self._wiki_loop())

    async def _wiki_loop(self):
        while True:
            try:
                await self._maybe_sync_wiki()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[slang] 梗库定时任务异常: {exc}")
            await asyncio.sleep(60)

    async def _maybe_sync_wiki(self):
        if not self.config.get("wiki_sync_enable", False) or self._wiki_running:
            return
        if not self.wiki_enable:
            return
        hhmm = str(self.config.get("wiki_sync_time") or "04:30")
        if time.strftime("%H:%M") != hhmm:
            return
        try:
            interval = max(1, int(self.config.get("wiki_sync_interval_days", 7) or 7))
        except (TypeError, ValueError):
            interval = 7
        last = float(self.store.meta_get("wiki_synced_at") or 0)
        if last and time.time() - last < interval * 86400:
            return
        self._wiki_running = True
        try:
            from .wikinow import sync as sync_wiki
            res = await asyncio.to_thread(
                sync_wiki, self.store, 30, 0.5, 0, False, logger.info)
            self._wiki_keys = self.store.wiki_keys()
            logger.info(f"[slang] 梗库自动同步完成: {res}")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[slang] 梗库自动同步失败: {exc}")
        finally:
            self._wiki_running = False

    async def terminate(self):
        try:
            if self._wiki_task:
                self._wiki_task.cancel()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 工具 ----------------

    @staticmethod
    def _split_list(raw) -> list[str]:
        """逗号/顿号/分号/空白都算分隔符，中英文都支持。"""
        if not raw:
            return []
        items = raw if isinstance(raw, (list, tuple, set)) else \
            re.split(r"[,，、;；\s]+", str(raw))
        out = []
        for x in items:
            x = str(x).strip()
            if x and x not in out:
                out.append(x)
        return out

    @staticmethod
    def _parse_list(raw) -> set[str]:
        return set(JargonLite._split_list(raw))

    def _scope(self, event: AstrMessageEvent) -> str:
        try:
            gid = getattr(event.message_obj, "group_id", None)
            if gid:
                return str(gid)
        except Exception:  # noqa: BLE001
            pass
        umo = event.unified_msg_origin or ""
        return umo.split(":")[-1] if umo else ""

    def _blocked(self, event: AstrMessageEvent) -> bool:
        if not self.blacklist:
            return False
        sid = self._scope(event)
        return sid in self.blacklist

    # ---------------- 学习侧 ----------------

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def collect_group(self, event: AstrMessageEvent):
        await self._collect(event)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def collect_private(self, event: AstrMessageEvent):
        if self.listen_private:
            await self._collect(event)

    async def _collect(self, event: AstrMessageEvent):
        if not self.enabled or not self.auto_learn:
            return
        if self._blocked(event):
            return
        if event.get_sender_id() == event.get_self_id():
            return
        text = (event.get_message_str() or "").strip()
        if not text or text.startswith("/"):
            return

        umo = event.unified_msg_origin or ""
        scope = self._scope(event)
        try:
            sender = event.get_sender_name() or event.get_sender_id()
        except Exception:  # noqa: BLE001
            sender = event.get_sender_id()

        count = self.store.buffer_add(umo, sender, text)
        if count < self.learn_frequency:
            return
        if umo in self._learning:
            return

        messages = self.store.buffer_take(umo, self.learn_frequency)
        self.store.buffer_drop(umo)
        self._learning.add(umo)
        asyncio.create_task(self._learn(umo, scope, messages))

    async def _learn(self, umo: str, scope: str, messages: list[dict]):
        lock = self._locks.setdefault(umo, asyncio.Lock())
        try:
            async with lock:
                n = await self.miner.learn(umo, scope, messages)
                logger.info(f"[slang] {scope} 本轮入库 {n} 条")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[slang] 学习异常: {exc}")
        finally:
            self._learning.discard(umo)

    # ---------------- 注入侧 ----------------

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req):
        if not self.enabled:
            return
        if self._blocked(event):
            return
        text = (getattr(req, "prompt", "") or "").strip()
        if not text:
            return
        entries = self.store.eligible(self._scope(event))
        hits = match(entries, text, limit=self.inject_max) if entries else []
        hint, tag = "", ""
        if hits:
            hint = format_hint(hits, self.max_meaning,
                               budget=self.max_meaning * self.inject_max + 120)
            if hint:
                tag = "黑话 " + ", ".join(h.get("name", "") for h in hits)
        # 黑话没命中，再看梗库（最多 1 条，避免抢上下文）
        if not hint and self.wiki_enable and self._wiki_keys:
            try:
                titles = match_wiki(self._wiki_keys, text, limit=1)
                if titles:
                    hint = format_wiki(self.store.wiki_get(titles[0]))
                    tag = f"梗 {titles[0]}"
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[slang] 梗库查询失败: {exc}")
        if not hint:
            return
        if TextPart is None:
            logger.warning("[slang] 当前 AstrBot 不支持临时上下文，跳过注入")
            return
        # 作为临时 user content 追加，不修改 system_prompt，避免破坏缓存
        req.extra_user_content_parts.append(TextPart(text=hint).mark_as_temp())
        logger.debug(f"[slang] 注入{tag}")

    # ---------------- WebUI 审核台 ----------------

    def _register_web_api(self):
        if self._web_registered or web_request is None:
            return
        if not hasattr(self.context, "register_web_api"):
            logger.warning("[slang] 当前 AstrBot 不支持插件页面，跳过")
            return
        prefix = "/astrbot_plugin_slang/yh"
        routes = [
            ("list", self.api_list, ["GET", "POST"], "词条列表"),
            ("ping", self.api_ping, ["GET", "POST"], "连通性自检"),
            ("groups", self.api_groups, ["GET", "POST"], "群列表"),
            ("tree", self.api_tree, ["GET", "POST"], "侧栏树"),
            ("add", self.api_add, ["POST"], "手动录入"),
            ("update", self.api_update, ["POST"], "改含义"),
            ("delete", self.api_delete, ["POST"], "删除"),
            ("approve", self.api_approve, ["POST"], "通过/退回"),
            ("toggle_global", self.api_toggle_global, ["POST"], "切换全局"),
            ("switch", self.api_switch, ["POST"], "总开关"),
            ("config", self.api_config, ["GET", "POST"], "读写配置"),
        ]
        for name, handler, methods, desc in routes:
            try:
                self.context.register_web_api(f"{prefix}/{name}", handler, methods, desc)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[slang] 注册路由失败 {name}: {exc}")
                return
        self._web_registered = True

    @staticmethod
    async def _payload() -> dict:
        data = {}
        try:
            data = await web_request.json(default={}) or {}
        except TypeError:
            try:
                data = await web_request.json() or {}
            except Exception:  # noqa: BLE001
                data = {}
        except Exception:  # noqa: BLE001
            data = {}
        if not isinstance(data, dict):
            data = {}
        try:
            for key in ("content", "meaning", "search", "kind", "page", "size"):
                val = web_request.query.get(key)
                if val is not None and key not in data:
                    data[key] = val
        except Exception:  # noqa: BLE001
            pass
        return data

    def _serialize(self, rec: dict) -> dict:
        return {
            "id": rec["id"],
            "content": rec["name"],
            "meaning": rec["meaning"],
            "count": rec["count"],
            "is_global": rec["scope"] == "",
            "created_by": "manual" if rec["source"] == "manual" else "ai",
            "scopes": {rec["scope"]: rec["count"]} if rec["scope"] else {},
            "evidence": self._json_list(rec["evidence"]),
            "auto_note": rec["auto_note"],
            "source": rec["source"],
            "scope": rec["scope"],
            "updated_at": rec["updated_at"],
        }

    @staticmethod
    def _json_list(raw) -> list:
        try:
            data = json.loads(raw or "[]")
            return data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            return []

    def _stats_web(self) -> dict:
        s = self.store.stats()
        return {"total": s["total"], "learned": s["auto"], "manual": s["manual"],
                "pending": s["pending"], "by_category": s["by_category"]}

    async def _group_names(self) -> dict:
        """通过 AstrBot 的 DB 接口读取群名，读不到就返回空。"""
        try:
            aliases = await self.context.get_db().get_umo_aliases()
        except Exception:  # noqa: BLE001
            return {}
        out = {}
        for a in aliases:
            umo = getattr(a, "umo", "") or ""
            if ":GroupMessage:" not in umo:
                continue
            gid = umo.rsplit(":", 1)[-1]
            name = (getattr(a, "user_alias", "") or "").strip() or \
                   (getattr(a, "auto_name", "") or "").strip()
            if name:
                out[gid] = name
        return out

    async def api_groups(self):
        _, rows = self.store.query("all", size=2000)
        counts: dict[str, int] = {}
        for r in rows:
            if r["scope"]:
                counts[r["scope"]] = counts.get(r["scope"], 0) + 1
        names = await self._group_names()
        data = [{"scope": s, "name": names.get(s, ""), "count": n}
                for s, n in counts.items()]
        data.sort(key=lambda x: -x["count"])
        return {"status": "ok", "message": "", "data": {"groups": data}}

    async def api_ping(self):
        """页面自检：确认路由与数据库是否正常。"""
        try:
            s = self.store.stats()
            cols = [r[1] for r in self.store._conn.execute("PRAGMA table_info(jargon)")]
            build = time.strftime(
                "%m-%d %H:%M:%S",
                time.localtime(pathlib.Path(__file__).stat().st_mtime))
            return {"status": "ok", "message": "", "data": {
                "plugin": "astrbot_plugin_slang",
                "rows": s["total"], "pending": s["pending"],
                "enabled": self.enabled, "build": build,
                "wiki": self.store.wiki_count(),
            }}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": f"自检失败: {exc}", "data": {}}

    async def api_tree(self):
        _, rows = self.store.query("all", size=3000)
        names = await self._group_names()

        def bucket(pred):
            sub = [r for r in rows if pred(r)]
            counts: dict[str, int] = {}
            for r in sub:
                counts[r["scope"] or ""] = counts.get(r["scope"] or "", 0) + 1
            subs = [{"scope": k, "name": names.get(k, ""), "count": v}
                    for k, v in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
            return len(sub), subs

        m_total, m_subs = bucket(lambda r: r["source"] == "manual")
        a_total, a_subs = bucket(lambda r: r["source"] == "ai")
        p_total = self.store.pending_count()
        p_subs = [{"scope": s["scope"], "name": names.get(s["scope"], ""),
                   "count": s["count"]} for s in self.store.pending_scopes()]
        buckets = [
            {"key": "manual", "label": "手动", "count": m_total, "subs": m_subs},
            {"key": "auto", "label": "黑话库", "count": a_total, "subs": a_subs},
            {"key": "pending", "label": "待审", "count": p_total, "subs": p_subs},
        ]
        if self.wiki_enable:
            buckets.append({"key": "wiki", "label": "梗百科",
                            "count": self.store.wiki_count(), "subs": [],
                            "readonly": True})
        return {"status": "ok", "message": "", "data": {
            "total": len(rows), "buckets": buckets}}

    async def api_list(self):
        p = await self._payload()
        search = str(p.get("search") or "").strip()
        bucket = str(p.get("bucket") or "")
        try:
            page = max(1, int(p.get("page") or 1))
            size = min(200, max(1, int(p.get("size") or 50)))
        except (TypeError, ValueError):
            page, size = 1, 50
        sort = "count" if p.get("sort") == "count" else "recent"
        scope = str(p.get("scope") or "").strip()

        if bucket == "pending":
            ptotal, rows = self.store.pending_query(search, page, size, scope, sort)
            items = [{
                "id": f"p{r['id']}", "content": r["name"], "meaning": r["meaning"],
                "count": r["count"], "is_global": r["scope"] == "",
                "auto_note": r["auto_note"], "source": r["source"],
                "scope": r["scope"], "pending": True,
                "evidence": self._json_list(r["evidence"]),
                "updated_at": r["updated_at"],
            } for r in rows]
            return {"status": "ok", "message": "", "data": {
                "enabled": self.enabled, "stats": self._stats_web(),
                "total": ptotal, "page": page, "size": size, "items": items}}

        if bucket == "wiki":
            if not self.wiki_enable:
                return {"status": "ok", "message": "", "data": {
                    "enabled": self.enabled, "stats": self._stats_web(),
                    "total": 0, "page": 1, "size": 50, "items": []}}
            wtotal, wrows = self.store.wiki_query(search, page, size)
            items = [{
                "id": f"w{r['id']}", "content": r["title"], "meaning": r["summary"],
                "count": 0, "is_global": True, "auto_note": "",
                "source": "wiki", "scope": "", "read_only": True,
                "tags": r["tags"], "evidence": [], "updated_at": r["updated_at"],
            } for r in wrows]
            return {"status": "ok", "message": "", "data": {
                "enabled": self.enabled, "stats": self._stats_web(),
                "total": wtotal, "page": page, "size": size, "items": items}}

        kind = {"manual": "manual", "auto": "auto"}.get(bucket, "all")
        total, items = self.store.query_page(kind, search, scope, page, size, sort)
        return {"status": "ok", "message": "", "data": {
            "enabled": self.enabled, "stats": self._stats_web(),
            "total": total, "page": page, "size": size,
            "items": [self._serialize(r) for r in items]}}

    async def api_add(self):
        p = await self._payload()
        content = str(p.get("content") or "").strip()[:32]
        meaning = str(p.get("meaning") or "").strip()[:500]
        scope = str(p.get("scope") or "").strip()
        if scope == "__global__":
            scope = ""
        if not content or not meaning:
            return {"status": "error", "message": "词条和含义都不能为空", "data": {}}
        self.store.add_manual(content, meaning, scope=scope)
        where = "全局" if not scope else f"群 {scope}"
        return {"status": "ok", "message": f"「{content}」已录入（{where}）", "data": {}}

    async def api_update(self):
        p = await self._payload()
        content = str(p.get("content") or "").strip()
        name = str(p.get("name") or "").strip()
        meaning = str(p.get("meaning") or "").strip()
        if not content or not meaning:
            return {"status": "error", "message": "词条和含义都不能为空", "data": {}}
        scope = None
        if "scope" in p:
            scope = str(p.get("scope") or "").strip()
            if scope == "__global__":
                scope = ""
        old_scope = str(p.get("old_scope")).strip() if p.get("old_scope") is not None else None
        if old_scope == "__global__":
            old_scope = ""
        table = "pending" if self._in_pending(content, old_scope) else "jargon"
        cur = self.store._find(table, content, old_scope)
        rec = self.store.update(table, str(cur["id"]) if cur else content,
                                name or None, meaning[:500], scope)
        return {"status": "ok" if rec else "error",
                "message": f"「{name or content}」已更新" if rec else "改不成，可能重名",
                "data": {}}

    def _in_pending(self, key: str, scope: str | None = None) -> bool:
        sql = "SELECT 1 FROM pending WHERE (name = ? OR CAST(id AS TEXT) = ?)"
        params: list = [key, key]
        if scope is not None:
            sql += " AND scope = ?"
            params.append(scope)
        with self.store._lock:
            return bool(self.store._conn.execute(
                sql + " LIMIT 1", params).fetchone())

    async def api_delete(self):
        p = await self._payload()
        content = str(p.get("content") or "").strip()
        if not content:
            return {"status": "error", "message": "缺少词条", "data": {}}
        scope = None
        if "scope" in p:
            scope = str(p.get("scope") or "").strip()
            if scope == "__global__":
                scope = ""
        table = ("pending" if p.get("pending") else "jargon") if "pending" in p else None
        ok = self.store.delete(content, scope, table)
        return {"status": "ok" if ok else "error",
                "message": f"已删除「{content}」" if ok else f"没找到「{content}」", "data": {}}

    async def api_approve(self):
        p = await self._payload()
        content = str(p.get("content") or "").strip()
        if not content:
            return {"status": "error", "message": "缺少词条", "data": {}}
        scope = None
        if "scope" in p:
            scope = str(p.get("scope") or "").strip()
            if scope == "__global__":
                scope = ""
        only = "pending" if p.get("pending") else ("jargon" if p.get("from_jargon") else None)
        action = self.store.approve(content, scope, only)
        if action == "approved":
            return {"status": "ok", "message": f"「{content}」已通过，移入黑话库", "data": {}}
        if action == "pending":
            return {"status": "ok", "message": f"「{content}」已驳回，移回待审", "data": {}}
        return {"status": "error", "message": f"没找到「{content}」", "data": {}}

    async def api_toggle_global(self):
        p = await self._payload()
        content = str(p.get("content") or "").strip()
        rec = self.store.get(content)
        if not rec:
            return {"status": "error", "message": f"没找到「{content}」", "data": {}}
        if rec["scope"]:
            self.store.set_scope(content, "")
            return {"status": "ok", "message": f"「{content}」已设为全局", "data": {}}
        return {"status": "ok", "message": f"「{content}」当前已是全局", "data": {}}

    async def api_switch(self):
        p = await self._payload()
        self.enabled = bool(p.get("enabled"))
        try:
            if hasattr(self.config, "save_config"):
                self.config["enabled"] = self.enabled
                self.config.save_config()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[slang] 开关写入失败: {exc}")
        return {"status": "ok",
                "message": f"已{'开启' if self.enabled else '关闭'}",
                "data": {"enabled": self.enabled}}

    @staticmethod
    def _schema_fields() -> list[dict]:
        """直接读 _conf_schema.json，保证设置页和后台配置同源。"""
        try:
            raw = json.loads((pathlib.Path(__file__).parent / "_conf_schema.json")
                             .read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
        fields = []
        for key, spec in raw.items():
            fields.append({
                "key": key,
                "label": spec.get("description", key),
                "type": spec.get("type", "string"),
                "default": spec.get("default"),
                "min": spec.get("min"),
                "max": spec.get("max"),
            })
        return fields

    def _apply_config(self):
        self.enabled = bool(self.config.get("enabled", True))
        self.auto_learn = bool(self.config.get("auto_learn", True))
        self.learn_frequency = max(5, int(self.config.get("learn_frequency", 50)))
        self.inject_max = max(1, int(self.config.get("inject_max", 3)))
        self.max_meaning = max(20, int(self.config.get("max_meaning_chars", 200)))
        self.wiki_enable = bool(self.config.get("wiki_enable", True))
        self.listen_private = bool(self.config.get("listen_private", False))
        self.blacklist = self._parse_list(self.config.get("blacklist_groups", ""))
        try:
            self.miner.extract_max = max(1, int(self.config.get("extract_max", 5)))
        except Exception:  # noqa: BLE001
            pass

    async def api_config(self):
        p = await self._payload()
        fields = self._schema_fields()
        changed = []
        if p:
            for f in fields:
                key, typ = f["key"], f["type"]
                if key not in p:
                    continue
                raw = p[key]
                if typ == "bool":
                    self.config[key] = bool(raw)
                elif typ == "int":
                    try:
                        v = int(raw)
                    except (TypeError, ValueError):
                        continue
                    if f.get("min") is not None:
                        v = max(int(f["min"]), v)
                    if f.get("max") is not None:
                        v = min(int(f["max"]), v)
                    self.config[key] = v
                else:
                    val = str(raw if raw is not None else "").strip()
                    if key == "blacklist_groups":
                        # 统一存成英文逗号，去重，去空白
                        val = ",".join(self._split_list(val))
                    self.config[key] = val
                changed.append(key)
            if changed:
                self._apply_config()
                if hasattr(self.config, "save_config"):
                    try:
                        self.config.save_config()
                    except Exception:  # noqa: BLE001
                        pass
        providers = []
        try:
            for prov in self.context.get_all_providers():
                meta = prov.meta()
                providers.append({"id": meta.id, "model": meta.model or "",
                                  "type": meta.type or ""})
        except Exception:  # noqa: BLE001
            pass
        values = {f["key"]: self.config.get(f["key"], f["default"]) for f in fields}
        values["blacklist_groups"] = ",".join(
            self._split_list(values.get("blacklist_groups", "")))
        return {"status": "ok", "message": "已保存" if changed else "", "data": {
            "fields": fields, "values": values, "providers": providers,
            "wiki_count": self.store.wiki_count(),
            "wiki_synced_at": self.store.meta_get("wiki_synced_at") or ""}}

    # ---------------- 指令 ----------------

    @filter.command_group("yh")
    def yh(self):
        """黑话库管理：列表、审核、录入、统计。仅管理员可用。"""
        pass

    @staticmethod
    def _tokens(event: AstrMessageEvent) -> list[str]:
        text = (event.get_message_str() or "").strip().lstrip("/").strip()
        return text.split()[1:] if len(text.split()) > 1 else []

    @staticmethod
    def _fmt(rec: dict) -> str:
        scope = "全局" if not rec["scope"] else f"群{rec['scope']}"
        src = "手动" if rec.get("source") == "manual" else "自动"
        return f"#{rec['id']} {rec['name']}：{rec['meaning'][:40]} [{src} {scope} ×{rec['count']}]"

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yh.command("list")
    async def kb_list(self, event: AstrMessageEvent):
        """列出词条，带审核标记与归属。"""
        tokens = self._tokens(event)
        page = int(tokens[1]) if len(tokens) > 1 and tokens[1].isdigit() else 1
        size = 8
        total, rows = self.store.query_page("all", page=page, size=size)
        if not rows:
            yield event.plain_result("还没有词条。")
            return
        lines = [f"词条第 {page} 页（共 {total} 条）："] + [self._fmt(r) for r in rows]
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yh.command("pending")
    async def kb_pending(self, event: AstrMessageEvent):
        """列出还没人工通过、等着你复核的条目。"""
        total, rows = self.store.pending_query(size=10)
        if not rows:
            yield event.plain_result("没有待复核的条目。")
            return
        items = []
        for r in rows:
            scope = "全局" if not r["scope"] else f"群{r['scope']}"
            items.append(f"#{r['id']} {r['name']}：{r['meaning'][:40]} "
                         f"[{scope} ×{r['count']}]")
        lines = [f"待复核 {total} 条（前 10）："] + items
        lines.append("用 /yh approve 名称 或 /yh del 名称 处理")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yh.command("search")
    async def kb_search(self, event: AstrMessageEvent):
        """按关键词搜词条名和含义。"""
        tokens = self._tokens(event)
        if len(tokens) < 2:
            yield event.plain_result("用法：/yh search 关键词")
            return
        kw = tokens[1]
        _, rows = self.store.query_page("all", search=kw, size=200)
        hit = rows
        if not hit:
            yield event.plain_result(f"没找到包含「{kw}」的条目。")
            return
        yield event.plain_result(f"命中 {len(hit)} 条：\n" + "\n".join(self._fmt(r) for r in hit[:10]))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yh.command("add")
    async def kb_add(self, event: AstrMessageEvent):
        """手动录入词条：/yh add global|group 名称 含义。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能录入。")
            return
        tokens = self._tokens(event)
        if len(tokens) < 4:
            yield event.plain_result("用法：/yh add global|group 名称 含义")
            return
        where, name, meaning = tokens[1], tokens[2], " ".join(tokens[3:])
        scope = "" if where == "global" else self._scope(event)
        rec = self.store.add_manual(name, meaning, scope=scope)
        yield event.plain_result(f"已录入并标记人工过关：\n{self._fmt(rec)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yh.command("del")
    async def kb_del(self, event: AstrMessageEvent):
        """删除词条：/yh del 名称。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能删除。")
            return
        tokens = self._tokens(event)
        if len(tokens) < 2:
            yield event.plain_result("用法：/yh del 名称")
            return
        ok = self.store.delete(tokens[1])
        yield event.plain_result("已删除。" if ok else "没找到这个条目。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yh.command("approve")
    async def kb_approve(self, event: AstrMessageEvent):
        """标记人工通过：/yh approve 名称。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能审核。")
            return
        tokens = self._tokens(event)
        if len(tokens) < 2:
            yield event.plain_result("用法：/yh approve 名称")
            return
        rec = self.store.approve(tokens[1])
        yield event.plain_result(
            "已通过，移入黑话库。" if rec == "approved"
            else ("已退回待审。" if rec == "pending" else "没找到这个条目。"))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yh.command("promote")
    async def kb_promote(self, event: AstrMessageEvent):
        """把群词条提升为全局：/yh promote 名称。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能改归属。")
            return
        tokens = self._tokens(event)
        if len(tokens) < 2:
            yield event.plain_result("用法：/yh promote 名称")
            return
        yield event.plain_result(
            "已提升为全局词条。" if self.store.set_scope(tokens[1], "") else "没找到这个条目。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yh.command("stat")
    async def kb_stat(self, event: AstrMessageEvent):
        """查看词条总数与审核状态分布。"""
        s = self.store.stats()
        cats = "、".join(f"{k} {v}" for k, v in s["by_category"].items()) or "无"
        yield event.plain_result(
            f"黑话 {s['total']} 条（{cats}）\n"
            f"手动 {s['manual']} · 自动 {s['auto']} · 待审 {s['pending']}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yh.command("check")
    async def kb_check(self, event: AstrMessageEvent):
        """预览一句话会注入哪些词条：/yh check 一句话。"""
        tokens = self._tokens(event)
        if len(tokens) < 2:
            yield event.plain_result("用法：/yh check 一句话（看会注入什么）")
            return
        text = " ".join(tokens[1:])
        entries = self.store.eligible(self._scope(event))
        hits = match(entries, text, limit=self.inject_max)
        if not hits:
            yield event.plain_result("这句话不会命中任何词条。")
            return
        yield event.plain_result("会注入：\n" + "\n".join(
            f"· {h['name']}（命中「{h['_hit']}」）" for h in hits))
