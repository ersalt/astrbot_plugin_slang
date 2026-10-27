# -*- coding: utf-8 -*-
"""群黑话学习与注入（轻量版）。

设计要点：
- 消息缓冲落 SQLite，重启不丢，不靠内存计数
- 提取后先自审，再决定是否标“自审过关”，不拦人工复核
- 注入走关键词命中，最多 3 条，纯机械匹配，不调模型
"""

import asyncio
import json
import pathlib
import re
import shutil
import sqlite3
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:  # 旧版 AstrBot 无 web 模块
    from astrbot.api.web import request as web_request
except Exception:  # noqa: BLE001
    web_request = None

from .matcher import format_hint, format_wiki, match, match_wiki
from .miner import Miner
from .store import JargonStore


@register(
    "astrbot_plugin_slang",
    "贰叔",
    "群黑话学习与注入：提取 + 自审 + 人工复核，按群隔离",
    "1.0.0",
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
        self.inject_pending = bool(self.config.get("inject_pending", False))
        self.listen_private = bool(self.config.get("listen_private", False))
        self.blacklist = self._parse_list(self.config.get("blacklist_groups", ""))

        data_dir = StarTools.get_data_dir("astrbot_plugin_slang")
        data_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_data(data_dir)
        self.store = JargonStore(str(data_dir / "jargon.db"))
        self.miner = Miner(context, self.store, self.config)
        try:
            self._wiki_keys = self.store.wiki_keys()
        except Exception:  # noqa: BLE001
            self._wiki_keys = []
        self._locks: dict[str, asyncio.Lock] = {}
        self._learning: set[str] = set()
        self._web_registered = False
        self._register_web_api()

    @staticmethod
    def _migrate_legacy_data(data_dir: pathlib.Path):
        """改名后首次启动：把旧模块名的数据库搬过来。"""
        try:
            old = data_dir.parent / "astrbot_plugin_jargon_lite"
            target = data_dir / "jargon.db"
            if target.exists() or not old.is_dir():
                return
            for suffix in ("", "-wal", "-shm"):
                src = old / f"jargon.db{suffix}"
                if src.exists():
                    shutil.copy2(src, data_dir / f"jargon.db{suffix}")
            logger.info("[slang] 已从旧数据目录迁移 jargon.db")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[slang] 旧数据迁移失败: {exc}")

    async def initialize(self):
        self._seed_from_file()
        try:
            self._wiki_keys = self.store.wiki_keys()
        except Exception:  # noqa: BLE001
            self._wiki_keys = []
        s = self.store.stats()
        logger.info(
            f"[slang] 已加载：共 {s['total']} 条"
            f"（手动 {s['manual']}，自审过关 {s['auto_ok']}，待复核 {s['pending']}）"
            f"，梗库 {len(self._wiki_keys)} 条")

    def _seed_from_file(self):
        """首次启动时从插件目录的 seed.txt 导入（key=value，一行一条）。"""
        try:
            if self.store.stats()["total"] > 0:
                return
            path = pathlib.Path(__file__).parent / "seed.txt"
            if not path.exists():
                return
            n = 0
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or "=" not in line or line.startswith("#"):
                    continue
                name, meaning = line.split("=", 1)
                if name.strip() and meaning.strip():
                    self.store.add_manual(name.strip(), meaning.strip(), scope="")
                    n += 1
            logger.info(f"[slang] 从 seed.txt 导入 {n} 条")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[slang] seed 导入失败: {exc}")

    async def terminate(self):
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 工具 ----------------

    @staticmethod
    def _parse_list(raw) -> set[str]:
        if not raw:
            return set()
        items = raw if isinstance(raw, (list, tuple, set)) else re.split(r"[,，;\s]+", str(raw))
        return {str(x).strip() for x in items if str(x).strip()}

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
        entries = self.store.eligible(self._scope(event), self.inject_pending)
        hits = match(entries, text, limit=self.inject_max) if entries else []
        if hits:
            hint = format_hint(hits, self.max_meaning,
                               budget=self.max_meaning * self.inject_max + 120)
            if hint:
                sep = "\n\n" if req.system_prompt else ""
                req.system_prompt = (req.system_prompt or "") + sep + hint
                logger.debug(f"[slang] 注入黑话 {len(hits)} 条: "
                             + ", ".join(h.get("name", "") for h in hits))
            return
        # 黑话没命中，再看梗库（最多 1 条，避免抢上下文）
        if not self._wiki_keys:
            return
        try:
            titles = match_wiki(self._wiki_keys, text, limit=1)
            if not titles:
                return
            hint = format_wiki(self.store.wiki_get(titles[0]))
            if hint:
                sep = "\n\n" if req.system_prompt else ""
                req.system_prompt = (req.system_prompt or "") + sep + hint
                logger.debug(f"[slang] 注入梗 {titles[0]}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[slang] 梗库注入失败: {exc}")

    # ---------------- WebUI 审核台 ----------------

    def _register_web_api(self):
        if self._web_registered or web_request is None:
            return
        if not hasattr(self.context, "register_web_api"):
            logger.warning("[slang] 当前 AstrBot 不支持插件页面，跳过")
            return
        prefix = "/astrbot_plugin_slang/kb"
        routes = [
            ("list", self.api_list, ["GET", "POST"], "词条列表"),
            ("ping", self.api_ping, ["GET", "POST"], "连通性自检"),
            ("groups", self.api_groups, ["GET", "POST"], "群列表"),
            ("tree", self.api_tree, ["GET", "POST"], "侧栏树"),
            ("add", self.api_add, ["POST"], "手动录入"),
            ("update", self.api_update, ["POST"], "改含义"),
            ("delete", self.api_delete, ["POST"], "删除"),
            ("approve", self.api_approve, ["POST"], "人工通过"),
            ("set_review", self.api_set_review, ["POST"], "改审核状态"),
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
            "is_jargon": bool(rec["review_manual"] or rec["review_auto"]),
            "is_complete": bool(rec["review_manual"]),
            "is_global": rec["scope"] == "",
            "created_by": "manual" if rec["source"] == "manual" else "ai",
            "scopes": {rec["scope"]: rec["count"]} if rec["scope"] else {},
            "evidence": self._json_list(rec["evidence"]),
            "auto_review": bool(rec["review_auto"]),
            "manual_review": bool(rec["review_manual"]),
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
        return {"total": s["total"], "learned": s["manual"], "manual": s["manual"],
                "pending": s["pending"], "auto_ok": s["auto_ok"],
                "by_category": s["by_category"]}

    def _group_names(self) -> dict:
        """从 AstrBot 的 umo_aliases 读群名，读不到就返回空。"""
        try:
            data_dir = pathlib.Path(
                StarTools.get_data_dir("astrbot_plugin_slang")).parent.parent
            db = data_dir / "data_v4.db"
            if not db.exists():
                return {}
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT umo, auto_name, user_alias FROM umo_aliases").fetchall()
            finally:
                conn.close()
            out = {}
            for umo, auto, alias in rows:
                if ":GroupMessage:" not in (umo or ""):
                    continue
                gid = umo.rsplit(":", 1)[-1]
                name = (alias or "").strip() or (auto or "").strip()
                if name:
                    out[gid] = name
            return out
        except Exception:  # noqa: BLE001
            return {}

    async def api_groups(self):
        _, rows = self.store.query("all", size=2000)
        counts: dict[str, int] = {}
        for r in rows:
            if r["scope"]:
                counts[r["scope"]] = counts.get(r["scope"], 0) + 1
        names = self._group_names()
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
                "schema": "v2" if "scope" in cols else "v1",
                "rows": s["total"], "pending": s["pending"],
                "enabled": self.enabled, "build": build,
                "wiki": self.store.wiki_count(),
            }}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": f"自检失败: {exc}", "data": {}}

    async def api_tree(self):
        _, rows = self.store.query("all", size=3000)
        names = self._group_names()

        def bucket(pred):
            sub = [r for r in rows if pred(r)]
            counts: dict[str, int] = {}
            for r in sub:
                counts[r["scope"] or ""] = counts.get(r["scope"] or "", 0) + 1
            subs = [{"scope": k, "name": names.get(k, ""), "count": v}
                    for k, v in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
            return len(sub), subs

        g_total, _ = bucket(lambda r: not r["scope"])
        m_total, m_subs = bucket(lambda r: r["source"] == "manual")
        c_total, c_subs = bucket(lambda r: bool(r["scope"]))
        p_total, p_subs = bucket(lambda r: not r["review_manual"])
        return {"status": "ok", "message": "", "data": {
            "total": len(rows),
            "buckets": [
                {"key": "global", "label": "全局黑话", "count": g_total, "subs": []},
                {"key": "manual", "label": "手动黑话", "count": m_total, "subs": m_subs},
                {"key": "group", "label": "群黑话", "count": c_total, "subs": c_subs},
                {"key": "pending", "label": "待审黑话", "count": p_total, "subs": p_subs},
            ],
        }}

    async def api_list(self):
        p = await self._payload()
        search = str(p.get("search") or "").strip()
        kind = p.get("kind") if p.get("kind") in ("all", "pending", "auto", "manual") else "all"
        bucket = str(p.get("bucket") or "")
        try:
            page = max(1, int(p.get("page") or 1))
            size = min(200, max(1, int(p.get("size") or 50)))
        except (TypeError, ValueError):
            page, size = 1, 50
        _, rows = self.store.query(kind, size=3000)
        if bucket == "global":
            rows = [r for r in rows if not r["scope"]]
        elif bucket == "manual":
            rows = [r for r in rows if r["source"] == "manual"]
        elif bucket == "group":
            rows = [r for r in rows if r["scope"]]
        elif bucket == "pending":
            rows = [r for r in rows if not r["review_manual"]]
        scope = str(p.get("scope") or "").strip()
        if scope:
            rows = [r for r in rows if r["scope"] == scope]
        if search:
            rows = [r for r in rows if search in r["name"] or search in r["meaning"]]
        # 待复核排最前，其次自审过关，人工确认的沉底
        rows.sort(key=lambda r: (r["review_manual"], r["review_auto"], -r["count"]))
        total = len(rows)
        items = [self._serialize(r) for r in rows[(page - 1) * size: page * size]]
        return {"status": "ok", "message": "", "data": {
            "enabled": self.enabled, "stats": self._stats_web(),
            "total": total, "page": page, "size": size, "items": items}}

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
        target = content
        if name and name != content:
            rec = self.store.update_name(content, name)
            if not rec:
                return {"status": "error", "message": f"改不成「{name}」，可能重名", "data": {}}
            target = name
        if "scope" in p:
            scope = str(p.get("scope") or "").strip()
            if scope == "__global__":
                scope = ""
            if self.store.set_scope(target, scope) is None:
                return {"status": "error", "message": "改归属失败，可能重名", "data": {}}
        rec = self.store.update_meaning(target, meaning[:500])
        return {"status": "ok" if rec else "error",
                "message": f"「{target}」已更新" if rec else f"没找到「{target}」",
                "data": {}}

    async def api_delete(self):
        p = await self._payload()
        content = str(p.get("content") or "").strip()
        if not content:
            return {"status": "error", "message": "缺少词条", "data": {}}
        ok = self.store.delete(content)
        return {"status": "ok" if ok else "error",
                "message": f"已删除「{content}」" if ok else f"没找到「{content}」", "data": {}}

    async def api_approve(self):
        p = await self._payload()
        content = str(p.get("content") or "").strip()
        if not content:
            return {"status": "error", "message": "缺少词条", "data": {}}
        value = bool(p.get("value", True))
        rec = self.store.set_review(content, manual=value)
        if not rec:
            return {"status": "error", "message": f"没找到「{content}」", "data": {}}
        return {"status": "ok",
                "message": f"「{content}」已{'标记' if value else '取消'}人工通过", "data": {}}

    async def api_set_review(self):
        p = await self._payload()
        content = str(p.get("content") or "").strip()
        field = str(p.get("field") or "")
        value = bool(p.get("value"))
        if not content or field not in ("manual", "auto"):
            return {"status": "error", "message": "参数不对", "data": {}}
        rec = (self.store.set_review(content, manual=value) if field == "manual"
               else self.store.set_review(content, auto=value))
        if not rec:
            return {"status": "error", "message": f"没找到「{content}」", "data": {}}
        label = "人工通过" if field == "manual" else "自审过关"
        return {"status": "ok",
                "message": f"「{content}」已{'标记' if value else '取消'}{label}", "data": {}}

    async def api_toggle_global(self):
        p = await self._payload()
        content = str(p.get("content") or "").strip()
        rec = self.store.get(content)
        if not rec:
            return {"status": "error", "message": f"没找到「{content}」", "data": {}}
        if not rec["scope"]:
            self.store.demote(content)
            return {"status": "ok", "message": f"「{content}」已取消全局，不再注入", "data": {}}
        ok = self.store.promote(str(rec["id"]))
        return {"status": "ok" if ok else "error",
                "message": f"「{content}」已设为全局" if ok else "同名全局条目已存在",
                "data": {}}

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

    async def api_config(self):
        p = await self._payload()
        changed = []
        if p:
            for key, caster, lo, hi in (
                ("learn_frequency", int, 5, 10000),
                ("extract_max", int, 1, 100),
                ("inject_max", int, 1, 50),
            ):
                if key in p:
                    try:
                        self.config[key] = max(lo, min(hi, caster(p[key])))
                        changed.append(key)
                    except (TypeError, ValueError):
                        continue
            if "provider_id" in p:
                self.config["provider_id"] = str(p.get("provider_id") or "").strip()
                changed.append("provider_id")
            self.learn_frequency = max(5, int(self.config.get("learn_frequency", 50)))
            self.inject_max = max(1, int(self.config.get("inject_max", 3)))
            self.miner.extract_max = int(self.config.get("extract_max", 5))
            if changed and hasattr(self.config, "save_config"):
                try:
                    self.config.save_config()
                except Exception:  # noqa: BLE001
                    pass
        providers = []
        try:
            for prov in self.context.get_all_providers():
                meta = prov.meta()
                providers.append({"id": meta.id, "model": meta.model or "", "type": meta.type or ""})
        except Exception:  # noqa: BLE001
            pass
        return {"status": "ok", "message": "已保存" if p else "", "data": {
            "provider_id": str(self.config.get("provider_id") or ""),
            "learn_frequency": int(self.config.get("learn_frequency", 50)),
            "extract_max": int(self.config.get("extract_max", 5)),
            "inject_max": int(self.config.get("inject_max", 3)),
            "providers": providers}}

    # ---------------- 指令 ----------------

    @filter.command_group("kb")
    def kb(self):
        """黑话库管理：列表、审核、录入、统计。仅管理员可用。"""
        pass

    @staticmethod
    def _tokens(event: AstrMessageEvent) -> list[str]:
        text = (event.get_message_str() or "").strip().lstrip("/").strip()
        return text.split()[1:] if len(text.split()) > 1 else []

    @staticmethod
    def _fmt(rec: dict) -> str:
        scope = "全局" if not rec["scope"] else f"群{rec['scope']}"
        flags = ("人工✅" if rec["review_manual"] else "人工✗") + "/" + \
                ("自审✅" if rec["review_auto"] else "自审✗")
        return f"#{rec['id']} {rec['name']}：{rec['meaning'][:40]} [{flags} {scope} ×{rec['count']}]"

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kb.command("list")
    async def kb_list(self, event: AstrMessageEvent):
        """列出词条，带审核标记与归属。"""
        tokens = self._tokens(event)
        page = int(tokens[1]) if len(tokens) > 1 and tokens[1].isdigit() else 1
        size = 8
        total, rows = self.store.query("all", page=page, size=size)
        if not rows:
            yield event.plain_result("还没有词条。")
            return
        lines = [f"词条第 {page} 页（共 {total} 条）："] + [self._fmt(r) for r in rows]
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kb.command("pending")
    async def kb_pending(self, event: AstrMessageEvent):
        """列出还没人工通过、等着你复核的条目。"""
        total, rows = self.store.query("pending", size=10)
        if not rows:
            yield event.plain_result("没有待复核的条目。")
            return
        lines = [f"待复核 {total} 条（前 10）："] + [self._fmt(r) for r in rows]
        lines.append("用 /kb approve 名称 或 /kb del 名称 处理")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kb.command("search")
    async def kb_search(self, event: AstrMessageEvent):
        """按关键词搜词条名和含义。"""
        tokens = self._tokens(event)
        if len(tokens) < 2:
            yield event.plain_result("用法：/kb search 关键词")
            return
        kw = tokens[1]
        total, rows = self.store.query("all", size=200)
        hit = [r for r in rows if kw in r["name"] or kw in r["meaning"]]
        if not hit:
            yield event.plain_result(f"没找到包含「{kw}」的条目。")
            return
        yield event.plain_result(f"命中 {len(hit)} 条：\n" + "\n".join(self._fmt(r) for r in hit[:10]))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kb.command("add")
    async def kb_add(self, event: AstrMessageEvent):
        """手动录入词条：/kb add global|group 名称 含义。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能录入。")
            return
        tokens = self._tokens(event)
        if len(tokens) < 4:
            yield event.plain_result("用法：/kb add global|group 名称 含义")
            return
        where, name, meaning = tokens[1], tokens[2], " ".join(tokens[3:])
        scope = "" if where == "global" else self._scope(event)
        rec = self.store.add_manual(name, meaning, scope=scope)
        yield event.plain_result(f"已录入并标记人工过关：\n{self._fmt(rec)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kb.command("del")
    async def kb_del(self, event: AstrMessageEvent):
        """删除词条：/kb del 名称。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能删除。")
            return
        tokens = self._tokens(event)
        if len(tokens) < 2:
            yield event.plain_result("用法：/kb del 名称")
            return
        ok = self.store.delete(tokens[1])
        yield event.plain_result("已删除。" if ok else "没找到这个条目。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kb.command("approve")
    async def kb_approve(self, event: AstrMessageEvent):
        """标记人工通过：/kb approve 名称。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能审核。")
            return
        tokens = self._tokens(event)
        if len(tokens) < 2:
            yield event.plain_result("用法：/kb approve 名称")
            return
        rec = self.store.approve(tokens[1])
        yield event.plain_result(
            f"已标记人工过关：\n{self._fmt(rec)}" if rec else "没找到这个条目。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kb.command("promote")
    async def kb_promote(self, event: AstrMessageEvent):
        """把群词条提升为全局：/kb promote 名称。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能改归属。")
            return
        tokens = self._tokens(event)
        if len(tokens) < 2:
            yield event.plain_result("用法：/kb promote 名称")
            return
        yield event.plain_result(
            "已提升为全局词条。" if self.store.promote(tokens[1]) else "没找到，或同名全局条目已存在。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kb.command("stat")
    async def kb_stat(self, event: AstrMessageEvent):
        """查看词条总数与审核状态分布。"""
        s = self.store.stats()
        cats = "、".join(f"{k} {v}" for k, v in s["by_category"].items()) or "无"
        yield event.plain_result(
            f"词条 {s['total']} 条（{cats}）\n"
            f"人工过关 {s['manual']} · 自审过关待人工 {s['auto_ok']} · 待复核 {s['pending']}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kb.command("check")
    async def kb_check(self, event: AstrMessageEvent):
        """预览一句话会注入哪些词条：/kb check 一句话。"""
        tokens = self._tokens(event)
        if len(tokens) < 2:
            yield event.plain_result("用法：/kb check 一句话（看会注入什么）")
            return
        text = " ".join(tokens[1:])
        entries = self.store.eligible(self._scope(event), self.inject_pending)
        hits = match(entries, text, limit=self.inject_max)
        if not hits:
            yield event.plain_result("这句话不会命中任何词条。")
            return
        yield event.plain_result("会注入：\n" + "\n".join(
            f"· {h['name']}（命中「{h['_hit']}」）" for h in hits))
