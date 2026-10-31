# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""群语汇：群黑话学习与注入。

四张词条表（manual/auto/manual_pending/auto_pending）+ 自然键操作；
注入走内存关键词索引，不再每次全表读取。
"""

import asyncio
import json
import pathlib
import re
import shutil
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:
    from astrbot.api.web import request as web_request
except Exception:  # noqa: BLE001
    web_request = None

try:
    from astrbot.core.agent.message import TextPart
except Exception:  # noqa: BLE001
    TextPart = None

from .matcher import format_hint, format_wiki, match_wiki
from .miner import Miner, parse_words
from .service import SIDE, SlangError, SlangService
from .store import TABLES, JargonStore


def _read_version(default: str = "0.0.0") -> str:
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


def _flatten_config(raw) -> dict:
    """兼容 AstrBot 分组 schema 与旧版扁平配置。"""
    out = {}
    for key, value in (raw or {}).items():
        if isinstance(value, dict):
            out.update(_flatten_config(value))
        else:
            out[key] = value
    return out


def _schema_leaves() -> dict:
    """返回 {叶子键: (分组路径, 默认值)}。"""
    try:
        raw = json.loads((pathlib.Path(__file__).parent / "_conf_schema.json")
                         .read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    leaves = {}

    def walk(items, prefix):
        for key, spec in items.items():
            path = prefix + (key,)
            if spec.get("type") == "object" and isinstance(spec.get("items"), dict):
                walk(spec["items"], path)
            else:
                leaves[key] = (path, spec.get("default"))

    walk(raw, ())
    return leaves


_MISSING = object()


def _migrate_flat_config(config) -> None:
    """把旧版扁平键搬进新分组，避免 AstrBot 补默认值后覆盖用户配置。"""
    leaves = _schema_leaves()
    changed = False
    # 旧版布尔开关 -> 新版模式
    if "extract_no_think" in config:
        old = bool(config.pop("extract_no_think"))
        mode = "auto" if old else "off"
        learning = config.get("learning")
        if not isinstance(learning, dict):
            learning = {}
            config["learning"] = learning
        if not learning.get("extract_thinking") or learning.get("extract_thinking") == "auto":
            learning["extract_thinking"] = mode
        changed = True
    for key, (path, default) in leaves.items():
        if key not in config or isinstance(config.get(key), dict):
            continue
        flat_value = config[key]
        node = config
        for part in path[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        current = node.get(path[-1], _MISSING)
        if current is _MISSING or current == default:
            node[path[-1]] = flat_value
        config.pop(key, None)
        changed = True
    if changed and hasattr(config, "save_config"):
        try:
            config.save_config()
        except Exception:  # noqa: BLE001
            pass


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
        _migrate_flat_config(self.config)
        self.cfg = _flatten_config(self.config)

        self.enabled = bool(self.cfg.get("enabled", True))
        self.auto_learn = bool(self.cfg.get("auto_learn", True))
        self.learn_frequency = max(5, int(self.cfg.get("learn_frequency", 50)))
        self.learn_idle_seconds = max(0, int(self.cfg.get("learn_idle_seconds", 300)))
        self.learn_force_multiplier = max(0, int(self.cfg.get("learn_force_multiplier", 2)))
        self.learn_group_gap_seconds = max(0, int(self.cfg.get("learn_group_gap_seconds", 5)))
        self.inject_max = max(1, int(self.cfg.get("inject_max", 3)))
        self.max_meaning = max(20, int(self.cfg.get("max_meaning_chars", 200)))
        self.wiki_enable = bool(self.cfg.get("wiki_enable", True))
        self.listen_private = bool(self.cfg.get("listen_private", False))
        self.blacklist = self._parse_list(self.cfg.get("blacklist_groups", ""))

        data_dir = StarTools.get_data_dir("astrbot_plugin_slang")
        data_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_old_data_dir(data_dir)
        self.store = JargonStore(str(data_dir / "jargon.db"))
        self.svc = SlangService(self.store)
        self.miner = Miner(context, self.svc, self.cfg)
        try:
            self._wiki_keys = self.store.wiki_keys()
        except Exception:  # noqa: BLE001
            self._wiki_keys = []
        self._locks: dict[str, asyncio.Lock] = {}
        self._learning: set[str] = set()
        self._learn_pending: dict[str, str] = {}
        self._learn_cooldown: dict[str, float] = {}
        self._last_reply_at: dict[str, float] = {}
        self._learn_task = None
        self._wiki_task = None
        self._wiki_running = False
        self._web_registered = False
        self._inject = {"calls": 0, "hits": 0, "empty": 0,
                        "blocked": 0, "no_hit": 0, "last": ""}
        self._inject_seen = False
        self._register_web_api()

    @staticmethod
    def _migrate_old_data_dir(data_dir: pathlib.Path):
        """从更早的插件目录名接管数据库（1.0 之前叫 jargon_lite）。"""
        target = data_dir / "jargon.db"
        if target.exists():
            return
        for name in ("astrbot_plugin_jargon_lite", "astrbot_plugin_jargon"):
            old = data_dir.parent / name
            src = old / "jargon.db"
            if not src.exists():
                continue
            try:
                for suffix in ("", "-wal", "-shm"):
                    s = old / f"jargon.db{suffix}"
                    if s.exists():
                        shutil.copy2(s, data_dir / f"jargon.db{suffix}")
                logger.info(f"[slang] 已从旧目录 {name} 迁移数据库")
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[slang] 旧目录迁移失败: {exc}")

    async def initialize(self):
        try:
            self._wiki_keys = self.store.wiki_keys()
        except Exception:  # noqa: BLE001
            self._wiki_keys = []
        s = self.svc.stats()
        logger.info(
            f"[slang] 已加载：黑话 {s['total']} 条（手动 {s['manual']}，自动 {s['auto']}），"
            f"待审 {s['pending']}，梗库 {len(self._wiki_keys)} 条")
        self._wiki_task = asyncio.create_task(self._wiki_loop())

    # ---------------- 梗库定时同步 ----------------

    async def _wiki_loop(self):
        while True:
            try:
                await self._maybe_sync_wiki()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[slang] 梗库定时任务异常: {exc}")
            await asyncio.sleep(60)

    async def _maybe_sync_wiki(self):
        if not self.cfg.get("wiki_sync_enable", False) or self._wiki_running:
            return
        if not self.wiki_enable:
            return
        hhmm = str(self.cfg.get("wiki_sync_time") or "04:30")
        if time.strftime("%H:%M") != hhmm:
            return
        try:
            interval = max(1, int(self.cfg.get("wiki_sync_interval_days", 7) or 7))
        except (TypeError, ValueError):
            interval = 7
        last = float(self.store.meta_get("wiki_synced_at") or 0)
        if last and time.time() - last < interval * 86400:
            return
        self._wiki_running = True
        try:
            from .wikinow import sync as sync_wiki
            res = await asyncio.to_thread(sync_wiki, self.store, 30, 0.5, 0, False,
                                          logger.info)
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
            if self._learn_task:
                self._learn_task.cancel()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 工具 ----------------

    @staticmethod
    def _split_list(raw) -> list[str]:
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
        return event.unified_msg_origin or ""

    def _blocked(self, event: AstrMessageEvent) -> bool:
        """群消息看黑名单，非群消息看私聊开关。

        不能用 unified_msg_origin 的平台段判断：适配器显示名可能是
        「大肥鱼」而不是「aiocqhttp」，会导致所有群消息被误判成私聊。
        """
        group_id = getattr(getattr(event, "message_obj", None), "group_id", None)
        if group_id:
            return str(group_id) in self.blacklist
        return not self.listen_private

    # ---------------- 学习侧 ----------------

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def collect_group(self, event: AstrMessageEvent):
        await self._collect(event)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def collect_private(self, event: AstrMessageEvent):
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
        self._learn_pending[umo] = scope
        self._ensure_learn_scheduler()

    @filter.on_llm_response()
    async def note_bot_reply(self, event: AstrMessageEvent, response):
        """记录机器人最后一次回复，学习调度用它判断是否已空闲 120 秒。"""
        umo = event.unified_msg_origin or ""
        if umo:
            self._last_reply_at[umo] = time.time()

    def _ensure_learn_scheduler(self):
        if self._learn_task is None or self._learn_task.done():
            self._learn_task = asyncio.create_task(self._learn_scheduler())

    async def _learn_scheduler(self):
        """单 worker 串行学习：同一时间只跑一个群，避免多群同时抢模型。"""
        while True:
            try:
                await asyncio.sleep(5)
                if self._learning or not self._learn_pending:
                    continue
                now = time.time()
                ready = []
                for umo, scope in list(self._learn_pending.items()):
                    if now < self._learn_cooldown.get(umo, 0):
                        continue
                    count = self.store.buffer_count(umo)
                    if count < self.learn_frequency:
                        self._learn_pending.pop(umo, None)
                        continue
                    idle = now - self._last_reply_at.get(umo, 0)
                    # 距离机器人最后一次回复满 N 秒；缓冲达到配置倍数时强制启动
                    force_count = self.learn_frequency * self.learn_force_multiplier
                    forced = (self.learn_force_multiplier > 0
                              and count >= force_count)
                    if idle < self.learn_idle_seconds and not forced:
                        continue
                    ready.append((self._last_reply_at.get(umo, 0), umo, scope))
                if not ready:
                    continue
                ready.sort(key=lambda item: item[0])
                _, umo, scope = ready[0]
                self._learn_pending.pop(umo, None)
                messages = self.store.buffer_take(umo, self.learn_frequency)
                if not messages:
                    continue
                self._learning.add(umo)
                try:
                    await self._learn(umo, scope, messages)
                finally:
                    self._learning.discard(umo)
                if self.store.buffer_count(umo) >= self.learn_frequency:
                    self._learn_pending[umo] = scope
                await asyncio.sleep(self.learn_group_gap_seconds)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[slang] 学习调度异常: {exc}")
                await asyncio.sleep(max(5, self.learn_group_gap_seconds))

    async def _learn(self, umo: str, scope: str, messages: list[dict]):
        lock = self._locks.setdefault(umo, asyncio.Lock())
        try:
            async with lock:
                n = await self.miner.learn(umo, scope, messages)
            if n is None:
                # 全部批次失败：保留缓冲，冷却后重试
                self._learn_cooldown[umo] = time.time() + 300
                logger.warning(f"[slang] {scope} 学习未完成，缓冲保留，5 分钟后重试")
                return
            ids = [m["id"] for m in messages if "id" in m]
            self.store.buffer_delete(ids)
            logger.info(f"[slang] {scope} 本轮入库 {n} 条，已清理 {len(ids)} 条缓冲")
        except Exception as exc:  # noqa: BLE001
            self._learn_cooldown[umo] = time.time() + 300
            logger.error(f"[slang] 学习异常，缓冲保留: {exc}")
        finally:
            self._learning.discard(umo)

    # ---------------- 注入侧 ----------------

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req):
        self._inject["calls"] += 1
        if not self._inject_seen:
            self._inject_seen = True
            logger.info("[slang] 注入钩子已触发（之后按命中情况记录 INFO）")
        if not self.enabled:
            self._inject["blocked"] += 1
            return
        if self._blocked(event):
            self._inject["blocked"] += 1
            return
        text = (getattr(req, "prompt", "") or "").strip()
        if not text:
            self._inject["empty"] += 1
            return
        hint, tag = "", ""
        try:
            entries = self.svc.match(text, self._scope(event), self.inject_max)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[slang] 索引匹配失败: {exc}")
            entries = []
        if entries:
            hint = format_hint(entries, self.max_meaning,
                               budget=self.max_meaning * self.inject_max + 120)
            if hint:
                tag = "黑话 " + ", ".join(e.get("name", "") for e in entries)
        if not hint and self.wiki_enable and self._wiki_keys:
            try:
                titles = match_wiki(self._wiki_keys, text, limit=1)
                if titles:
                    hint = format_wiki(self.store.wiki_get(titles[0]))
                    tag = f"梗 {titles[0]}"
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[slang] 梗库查询失败: {exc}")
        if not hint:
            self._inject["no_hit"] += 1
            return
        if TextPart is None:
            logger.warning("[slang] 当前 AstrBot 不支持临时上下文，跳过注入")
            return
        req.extra_user_content_parts.append(TextPart(text=hint).mark_as_temp())
        self._inject["hits"] += 1
        self._inject["last"] = f"{tag} @ {time.strftime('%H:%M:%S')}"
        logger.info(f"[slang] 注入 {tag}")

    # ---------------- WebUI ----------------

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
            ("lookup", self.api_lookup, ["GET", "POST"], "录入前查重"),
            ("add", self.api_add, ["POST"], "手动录入"),
            ("update", self.api_update, ["POST"], "改词条"),
            ("delete", self.api_delete, ["POST"], "删除"),
            ("approve", self.api_approve, ["POST"], "通过/退回"),
            ("bulk", self.api_bulk, ["POST"], "批量审核"),
            ("toggle_global", self.api_toggle_global, ["POST"], "切换全局"),
            ("switch", self.api_switch, ["POST"], "总开关"),
            ("config", self.api_config, ["GET", "POST"], "读写配置"),
        ]
        for name, handler, methods, desc in routes:
            try:
                self.context.register_web_api(f"{prefix}/{name}", handler, methods, desc)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[slang] 注册接口 {name} 失败: {exc}")
        self._web_registered = True

    @staticmethod
    async def _payload() -> dict:
        if web_request is None:
            return {}
        try:
            data = await web_request.json(default={})
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001
            pass
        try:
            return dict(web_request.query) if web_request.query else {}
        except Exception:  # noqa: BLE001
            return {}

    def _stats_web(self) -> dict:
        return self.svc.stats()

    @staticmethod
    def _resolve_table(p: dict) -> str:
        table = str(p.get("table") or "").strip()
        if table in TABLES:
            return table
        # 兼容旧页面：只接受明确的 source + pending 组合，猜不出返回空
        if "pending" not in p:
            return ""
        source = str(p.get("source") or p.get("old_source") or "").strip()
        if source not in ("manual", "ai"):
            return ""
        if bool(p.get("pending")):
            return "manual_pending" if source == "manual" else "auto_pending"
        return "manual" if source == "manual" else "auto"

    def _resolve_edit_table(self, name: str, scope: str, source: str = "") -> str:
        """旧页面编辑没有 table：按自然键在四张表里找，唯一命中才改。"""
        cands = [t for t in TABLES if self.store.repo(t).find(name, scope)]
        if source in ("manual", "ai"):
            want = "manual" if source == "manual" else "auto"
            cands = [t for t in cands if t.startswith(want)]
        return cands[0] if len(cands) == 1 else ""

    @staticmethod
    def _error(where: str, exc: Exception) -> dict:
        try:
            logger.error(f"[slang] {where}失败: {exc}", exc_info=True)
        except Exception:  # noqa: BLE001
            pass
        return {"status": "error",
                "message": f"{where}失败：{type(exc).__name__}: {exc}",
                "data": {}}

    async def _group_names(self) -> dict:
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
        names = await self._group_names()
        merged: dict[str, int] = {}
        for t in ("manual", "auto", "manual_pending", "auto_pending"):
            for s in self.svc.scopes_of(t):
                merged[s["scope"]] = merged.get(s["scope"], 0) + s["count"]
        data = [{"scope": k, "name": names.get(k, ""), "count": v}
                for k, v in merged.items()]
        data.sort(key=lambda x: -x["count"])
        return {"status": "ok", "message": "", "data": {"groups": data}}

    async def api_ping(self):
        try:
            s = self.svc.stats()
            build = time.strftime("%m-%d %H:%M:%S",
                                  time.localtime(pathlib.Path(__file__).stat().st_mtime))
            return {"status": "ok", "message": "", "data": {
                "plugin": "astrbot_plugin_slang", "rows": s["total"],
                "pending": s["pending"], "enabled": self.enabled, "build": build,
                "wiki": self.store.wiki_count(),
            }}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": f"自检失败: {exc}", "data": {}}

    async def api_tree(self):
        names = await self._group_names()

        def subs(table):
            return [{"scope": s["scope"], "name": names.get(s["scope"], ""),
                     "count": s["count"]} for s in self.svc.scopes_of(table)]

        manual, auto, pending = self.svc.tree_counts()
        p_subs = [{"scope": s["scope"], "name": names.get(s["scope"], ""),
                   "count": s["count"]} for s in self.svc.pending_scopes()]
        buckets = [
            {"key": "manual", "label": "手动", "count": manual, "subs": subs("manual")},
            {"key": "auto", "label": "黑话库", "count": auto, "subs": subs("auto")},
            {"key": "pending", "label": "待审", "count": pending, "subs": p_subs},
        ]
        if self.wiki_enable:
            buckets.append({"key": "wiki", "label": "梗百科",
                            "count": self.store.wiki_count(), "subs": [],
                            "readonly": True})
        return {"status": "ok", "message": "", "data": {
            "total": manual + auto, "buckets": buckets}}

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
        scoped = bool(p.get("scoped"))
        scope = str(p.get("scope") or "").strip()

        if bucket == "wiki":
            if not self.wiki_enable:
                return {"status": "ok", "message": "", "data": {
                    "total": 0, "page": 1, "size": 50, "items": []}}
            total, rows = self.store.wiki_query(search, page, size)
            items = [{
                "table": "wiki", "source": "wiki", "pending": False,
                "id": f"wiki:{r['id']}", "content": r["title"],
                "meaning": r["summary"], "count": 0, "scope": "",
                "is_global": True, "aliases": "", "auto_note": "",
                "contributor": "", "tags": r["tags"], "read_only": True,
                "evidence": "[]", "updated_at": r["updated_at"],
            } for r in rows]
            return {"status": "ok", "message": "", "data": {
                "enabled": self.enabled, "stats": self._stats_web(),
                "total": total, "page": page, "size": size, "items": items}}

        total, items = self.svc.list_entries(bucket, search, scope, scoped,
                                             page, size, sort)
        return {"status": "ok", "message": "", "data": {
            "enabled": self.enabled, "stats": self._stats_web(),
            "total": total, "page": page, "size": size, "items": items}}

    async def api_lookup(self):
        p = await self._payload()
        name = str(p.get("name") or p.get("content") or "").strip()[:32]
        scope = str(p.get("scope") or "").strip()
        if scope == "__global__":
            scope = ""
        if not name:
            return {"status": "error", "message": "词条名不能为空", "data": {}}
        return {"status": "ok", "message": "", "data": {
            "items": self.svc.lookup(name, scope)}}

    async def api_add(self):
        p = await self._payload()
        content = str(p.get("content") or "").strip()[:32]
        meaning = str(p.get("meaning") or "").strip()[:1000]
        scope = str(p.get("scope") or "").strip()
        if scope == "__global__":
            scope = ""
        keywords = str(p.get("keywords") or p.get("aliases") or "").strip()
        limit = max(1, int(self.cfg.get("max_meaning_chars", 200)))
        if len(meaning) > limit:
            return {"status": "error",
                    "message": f"内容 {len(meaning)}/{limit} 字，请删减后再录入",
                    "data": {}}
        try:
            self.svc.add_manual(content, meaning, scope=scope,
                                keywords=keywords)
        except SlangError as exc:
            return {"status": "error", "message": str(exc), "data": {}}
        except Exception as exc:  # noqa: BLE001
            return self._error("操作", exc)
        where = "全局" if not scope else f"群 {scope}"
        msg = f"「{content}」已录入（{where}）"
        # 手动/自动隔离：自动区已有同名时仍允许手动新增，但必须说清楚没覆盖
        if (self.store.repo("auto").find(content, scope)
                or self.store.repo("auto_pending").find(content, scope)):
            msg += "；黑话库已有同名条目，未覆盖"
        return {"status": "ok", "message": msg, "data": {}}

    async def api_update(self):
        p = await self._payload()
        old_name = str(p.get("old_name") or p.get("content") or "").strip()
        old_scope = str(p.get("old_scope") or "").strip()
        name = str(p.get("name") or "").strip()
        meaning = str(p.get("meaning") or "").strip()
        scope = str(p.get("scope") or "").strip() if "scope" in p else None
        if scope == "__global__":
            scope = ""
        keywords = str(p.get("keywords") or "").strip() if "keywords" in p else None
        table = str(p.get("table") or "").strip()
        if table not in TABLES:
            table = self._resolve_edit_table(
                old_name, old_scope, str(p.get("old_source") or p.get("source") or ""))
        if table not in TABLES:
            return {"status": "error",
                    "message": "页面版本过旧，无法确定要修改哪张表。请按 Ctrl+F5 强制刷新后重试。",
                    "data": {}}
        limit = max(1, int(self.cfg.get("max_meaning_chars", 200)))
        if len(meaning) > limit:
            return {"status": "error",
                    "message": f"内容 {len(meaning)}/{limit} 字，请删减后再保存",
                    "data": {}}
        try:
            rec = self.svc.update(table, old_name, old_scope, name, meaning, scope,
                                  keywords=keywords)
        except SlangError as exc:
            return {"status": "error", "message": str(exc), "data": {}}
        except Exception as exc:  # noqa: BLE001
            return self._error("操作", exc)
        return {"status": "ok", "message": f"「{rec['name']}」已更新", "data": {}}

    async def api_delete(self):
        p = await self._payload()
        table = self._resolve_table(p)
        name = str(p.get("name") or p.get("content") or "").strip()
        scope = str(p.get("scope") or "").strip()
        try:
            self.svc.delete(table, name, scope)
        except SlangError as exc:
            return {"status": "error", "message": str(exc), "data": {}}
        except Exception as exc:  # noqa: BLE001
            return self._error("操作", exc)
        return {"status": "ok", "message": f"已删除「{name}」", "data": {}}

    async def api_approve(self):
        p = await self._payload()
        table = self._resolve_table(p)
        name = str(p.get("name") or p.get("content") or "").strip()
        scope = str(p.get("scope") or "").strip()
        try:
            action = self.svc.approve(table, name, scope)
        except SlangError as exc:
            return {"status": "error", "message": str(exc), "data": {}}
        except Exception as exc:  # noqa: BLE001
            return self._error("操作", exc)
        if action == "approved":
            target = SIDE["manual"] if table == "manual_pending" else SIDE["auto"]
            return {"status": "ok", "message": f"「{name}」已通过，移入{target}", "data": {}}
        target = SIDE["manual_pending"] if table == "manual" else SIDE["auto_pending"]
        return {"status": "ok", "message": f"「{name}」已驳回，移回{target}", "data": {}}

    async def api_bulk(self):
        p = await self._payload()
        action = str(p.get("action") or "")
        items = p.get("items")
        if action not in ("approve", "delete") or not isinstance(items, list) or not items:
            return {"status": "error", "message": "参数不对", "data": {}}
        try:
            res = self.svc.bulk(action, items[:200])
        except Exception as exc:  # noqa: BLE001
            return self._error("批量操作", exc)
        if action == "approve":
            msg = f"已通过 {res['approved']} 条"
        else:
            msg = f"已删除 {res['deleted']} 条待审"
        if res.get("skipped"):
            msg += f"；跳过 {res['skipped']} 条（同名或参数无效）"
        return {"status": "ok", "message": msg, "data": res}

    async def api_toggle_global(self):
        p = await self._payload()
        table = self._resolve_table(p)
        name = str(p.get("name") or p.get("content") or "").strip()
        scope = str(p.get("scope") or "").strip()
        try:
            if scope:
                self.svc.update(table, name, scope, scope="")
                return {"status": "ok", "message": f"「{name}」已设为全局", "data": {}}
        except SlangError as exc:
            return {"status": "error", "message": str(exc), "data": {}}
        except Exception as exc:  # noqa: BLE001
            return self._error("操作", exc)
        return {"status": "ok", "message": f"「{name}」已经是全局条目", "data": {}}

    def _set_config_path(self, path, value):
        node = self.config
        for part in path[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        if len(path) > 1:
            node.pop(path[-1], None)
        node[path[-1]] = value

    async def api_switch(self):
        p = await self._payload()
        self.enabled = bool(p.get("enabled"))
        self._set_config_path(("basic", "enabled"), self.enabled)
        if hasattr(self.config, "save_config"):
            try:
                self.config.save_config()
            except Exception:  # noqa: BLE001
                pass
        return {"status": "ok", "message": f"已{'开启' if self.enabled else '关闭'}",
                "data": {"enabled": self.enabled}}

    # ---------------- 配置 ----------------

    @staticmethod
    def _schema_fields() -> list[dict]:
        try:
            raw = json.loads((pathlib.Path(__file__).parent / "_conf_schema.json")
                             .read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
        fields = []

        def walk(items, prefix=()):
            for key, spec in items.items():
                typ = spec.get("type", "string")
                path = prefix + (key,)
                if typ == "object" and isinstance(spec.get("items"), dict):
                    walk(spec["items"], path)
                    continue
                group_key = prefix[0] if prefix else ""
                group_spec = raw.get(group_key) if group_key else {}
                fields.append({
                    "key": key, "path": list(path),
                    "group": spec.get("group") or group_key,
                    "group_label": (group_spec or {}).get("description") or group_key,
                    "label": spec.get("description", key),
                    "type": typ, "default": spec.get("default"),
                    "min": spec.get("min"), "max": spec.get("max"),
                    "options": spec.get("options"),
                    "labels": spec.get("labels"),
                    "condition": spec.get("condition"),
                    "hint": spec.get("hint"),
                })

        walk(raw)
        return fields

    def _apply_config(self):
        self.enabled = bool(self.cfg.get("enabled", True))
        self.auto_learn = bool(self.cfg.get("auto_learn", True))
        self.learn_frequency = max(5, int(self.cfg.get("learn_frequency", 50)))
        self.learn_idle_seconds = max(0, int(self.cfg.get("learn_idle_seconds", 300)))
        self.learn_force_multiplier = max(0, int(self.cfg.get("learn_force_multiplier", 2)))
        self.learn_group_gap_seconds = max(0, int(self.cfg.get("learn_group_gap_seconds", 5)))
        self.inject_max = max(1, int(self.cfg.get("inject_max", 3)))
        self.max_meaning = max(20, int(self.cfg.get("max_meaning_chars", 200)))
        self.wiki_enable = bool(self.cfg.get("wiki_enable", True))
        self.listen_private = bool(self.cfg.get("listen_private", False))
        self.blacklist = self._parse_list(self.cfg.get("blacklist_groups", ""))
        try:
            self.miner.extract_max = max(1, int(self.cfg.get("extract_max", 5)))
            self.miner.reject_words = parse_words(self.cfg.get("reject_words", ""))
            self.miner.batch_size = max(3, int(self.cfg.get("learn_batch_size", 15)))
            self.miner.batch_delay_ms = max(0, int(self.cfg.get("learn_batch_delay_ms", 600)))
            self.miner.batch_timeout = max(
                300, int(self.cfg.get("learn_batch_timeout", 300)))
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
                path = tuple(f.get("path") or (key,))
                if typ == "bool":
                    self._set_config_path(path, bool(raw))
                elif typ == "int":
                    try:
                        v = int(raw)
                    except (TypeError, ValueError):
                        continue
                    if f.get("min") is not None:
                        v = max(int(f["min"]), v)
                    if f.get("max") is not None:
                        v = min(int(f["max"]), v)
                    self._set_config_path(path, v)
                else:
                    val = str(raw if raw is not None else "").strip()
                    if key == "blacklist_groups":
                        val = ",".join(self._split_list(val))
                    self._set_config_path(path, val)
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
        values = {f["key"]: self.cfg.get(f["key"], f["default"]) for f in fields}
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
    def _clip(text: str, limit: int) -> str:
        text = text or ""
        return text if len(text) <= limit else text[:limit].rstrip("，。；; ") + "…"

    @classmethod
    def _fmt(cls, rec: dict) -> str:
        name = rec.get("content") or rec.get("name") or ""
        meaning = rec.get("meaning") or ""
        scope = rec.get("scope") or ""
        source = rec.get("source") or ""
        where = "全局" if not scope else f"群{scope}"
        src = "手动" if source == "manual" else "自动"
        return f"{name}：{cls._clip(meaning, 40)} [{src} {where} ×{rec.get('count', 1)}]"

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yh.command("list")
    async def yh_list(self, event: AstrMessageEvent):
        """列出已过审词条。"""
        tokens = self._tokens(event)
        page = int(tokens[1]) if len(tokens) > 1 and tokens[1].isdigit() else 1
        rows = self.svc.all_approved()
        rows.sort(key=lambda r: -r["updated_at"])
        total = len(rows)
        page_rows = rows[(page - 1) * 8: page * 8]
        if not page_rows:
            yield event.plain_result("还没有词条。")
            return
        lines = [f"词条第 {page} 页（共 {total} 条）："] + [self._fmt(r) for r in page_rows]
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yh.command("pending")
    async def yh_pending(self, event: AstrMessageEvent):
        """列出待审词条。"""
        total, rows = self.svc.list_entries("pending", page=1, size=10)
        if not rows:
            yield event.plain_result("没有待审的条目。")
            return
        lines = [f"待审 {total} 条（前 10）："] + [self._fmt(r) for r in rows]
        lines.append("用 /yh approve 名称 或 /yh del 名称 处理")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yh.command("search")
    async def yh_search(self, event: AstrMessageEvent):
        """按关键词搜词条。"""
        tokens = self._tokens(event)
        if len(tokens) < 2:
            yield event.plain_result("用法：/yh search 关键词")
            return
        kw = tokens[1]
        rows = [r for r in self.svc.all_approved()
                if kw in r["content"] or kw in r["meaning"]]
        if not rows:
            yield event.plain_result(f"没找到包含「{kw}」的已审词条。")
            return
        yield event.plain_result(
            f"命中 {len(rows)} 条：\n" + "\n".join(self._fmt(r) for r in rows[:10]))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yh.command("add")
    async def yh_add(self, event: AstrMessageEvent):
        """手动录入：/yh add global|group 名称 含义。"""
        tokens = self._tokens(event)
        if len(tokens) < 4:
            yield event.plain_result("用法：/yh add global|group 名称 含义")
            return
        where, name, meaning = tokens[1], tokens[2], " ".join(tokens[3:])
        scope = "" if where == "global" else self._scope(event)
        try:
            self.svc.add_manual(name, meaning, scope=scope)
        except SlangError as exc:
            yield event.plain_result(str(exc))
            return
        yield event.plain_result(f"已录入：{name}（{'全局' if not scope else '本群'}）")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yh.command("del")
    async def yh_del(self, event: AstrMessageEvent):
        """删除词条：/yh del 名称。"""
        tokens = self._tokens(event)
        if len(tokens) < 2:
            yield event.plain_result("用法：/yh del 名称")
            return
        found = self.svc.find_any(tokens[1])
        if not found:
            yield event.plain_result("没找到这个条目。")
            return
        table, rec = found
        self.svc.delete(table, rec["name"], rec["scope"])
        yield event.plain_result("已删除。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yh.command("approve")
    async def yh_approve(self, event: AstrMessageEvent):
        """通过或驳回：/yh approve 名称。"""
        tokens = self._tokens(event)
        if len(tokens) < 2:
            yield event.plain_result("用法：/yh approve 名称")
            return
        found = self.svc.find_any(tokens[1])
        if not found:
            yield event.plain_result("没找到这个条目。")
            return
        table, rec = found
        try:
            action = self.svc.approve(table, rec["name"], rec["scope"])
        except SlangError as exc:
            yield event.plain_result(str(exc))
            return
        yield event.plain_result(
            "已通过，移入黑话库。" if action == "approved" else "已驳回，移回待审。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yh.command("promote")
    async def yh_promote(self, event: AstrMessageEvent):
        """设为全局：/yh promote 名称。"""
        tokens = self._tokens(event)
        if len(tokens) < 2:
            yield event.plain_result("用法：/yh promote 名称")
            return
        found = self.svc.find_any(tokens[1])
        if not found:
            yield event.plain_result("没找到这个条目。")
            return
        table, rec = found
        if not rec["scope"]:
            yield event.plain_result("这条已经是全局词条了。")
            return
        try:
            self.svc.update(table, rec["name"], rec["scope"], scope="")
        except SlangError as exc:
            yield event.plain_result(str(exc))
            return
        yield event.plain_result("已提升为全局词条。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yh.command("stat")
    async def yh_stat(self, event: AstrMessageEvent):
        """查看统计。"""
        s = self.svc.stats()
        d = self._inject
        yield event.plain_result(
            f"黑话 {s['total']} 条（手动 {s['manual']} · 自动 {s['auto']}）\n"
            f"待审 {s['pending']} · 梗库 {self.store.wiki_count()}\n"
            f"注入：调用 {d['calls']} · 命中 {d['hits']} · 空文本 {d['empty']} "
            f"· 无命中 {d['no_hit']} · 跳过 {d['blocked']}\n"
            f"最近：{d['last'] or '无'}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @yh.command("check")
    async def yh_check(self, event: AstrMessageEvent):
        """预览一句话会注入什么：/yh check 一句话。"""
        tokens = self._tokens(event)
        if len(tokens) < 2:
            yield event.plain_result("用法：/yh check 一句话（看会注入什么）")
            return
        text = " ".join(tokens[1:])
        hits = self.svc.match(text, self._scope(event), self.inject_max)
        if not hits:
            yield event.plain_result("这句话不会命中任何词条。")
            return
        yield event.plain_result(
            f"命中 {len(hits)} 条（聊天只显示前 40 字，注入按 max_meaning_chars 截断）：\n"
            + "\n".join(
                f"[{SIDE.get(h['table'], h['table'])}] {h['name']}：{self._clip(h['meaning'], 40)}"
                for h in hits))
