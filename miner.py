# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""提取 + 自审。LLM 调用通过 AstrBot provider，解析逻辑纯本地。"""

import asyncio
import copy
import json
import re

from astrbot.api import logger

EXTRACT_PROMPT = """你在整理一个 QQ 群的黑话表。下面是从这个群抓的一段聊天。

只输出 JSON 数组，不要解释，不要 markdown 代码块。没有黑话就输出 []。
每个元素三个字段：
{"name":"黑话本身","aliases":"同义叫法，逗号分隔，没有就空字符串","meaning":"它在这个群里指什么，一句话","evidence":"原文里最能说明用法的一句，20 字内"}

要求：
- name 2~12 个字，必须是原文里真实出现过的
- 只挑群里自造、或只有这个群才懂的词；普通词、人名、@、表情不要
- 一次性的玩笑、口误、临时造词不要收
- 如果是群友的昵称或 ID，含义就写「群友 XXX 的 ID」，不要写「某个人的代称」
- 最多 {n} 条

聊天：
{chat}

输出："""

REVIEW_PROMPT = """判断下面每条值不值得收录成「群黑话」。两个标准都要过：

一、含义要具体
- 具体：说得出它指的人/物/事/动作，例如「指群主的小号」「指把图 P 得很糊」
- 空泛：同义反复或泛指，例如「某种特定事物」「群内的代称」「一种说法」「有冲击力的风格」

二、用法要稳定
- 稳定：群里会反复用，脱离这句话也懂
- 一次性：只是这次聊天里的玩笑、口误、临时造词

只要有一条不达标，certain 就是 false。
注意：「群友 XXX 的 ID」「指群主的小号」这种算具体，可以通过；「某个人的代称」不算。

只输出 JSON 数组，不要解释：[{{"name":"X","certain":true,"reason":"不超过20字"}}]

条目：
{items}

输出："""

_FENCE = re.compile(r"```(?:json)?\s*|\s*```")

# 这些词一出现，基本等于没解释，直接判不通过（不依赖模型自觉）
VAGUE_WORDS = ("特定事物", "某种特定", "某种东西", "群内的代称", "一类说法",
               "特定的代称", "某些事物", "之类的说法", "某种冲击", "一种感觉")


def parse_json(text: str):
    if not text:
        return None
    cleaned = _FENCE.sub("", text).strip()
    for a, b in (("[", "]"), ("{", "}")):
        s, e = cleaned.find(a), cleaned.rfind(b)
        if s != -1 and e > s:
            try:
                return json.loads(cleaned[s:e + 1])
            except json.JSONDecodeError:
                continue
    return None


def _clean_name(s: str) -> str:
    s = (s or "").strip().strip("「」\"'“”《》")
    return s[:12]


def _clean_meaning(s: str, limit: int) -> str:
    s = " ".join((s or "").split())
    if len(s) > limit:
        s = s[:limit].rstrip("，。；; ") + "…"
    return s


def parse_words(raw) -> set[str]:
    """拒绝词表解析：中英文逗号、顿号、分号、空白都算分隔符。"""
    if not raw:
        return set()
    parts = re.split(r"[,，、;；\s]+", str(raw))
    return {p.strip().casefold() for p in parts if p.strip()}



_ID_WORDS = r"(?:QQ\s*)?(?:ID|id|Id|账号|号码|号)"


def is_low_value(name: str, meaning: str) -> bool:
    """判断“群友 XXX 的 ID”这类同义反复、没有新信息的词条。"""
    name = (name or "").strip()
    text = re.sub(r"\s+", " ", (meaning or "").strip())
    if not name or not text:
        return False
    n = re.escape(name)
    patterns = [
        rf"^(?:群友\s*)?{n}\s*(?:[（(][^）)]*[）)])?\s*(?:的\s*)?{_ID_WORDS}$",
        rf"^群友\s*{n}\s*$",
    ]
    return any(re.match(p, text) for p in patterns)


class Miner:
    def __init__(self, context, store, config):
        self.context = context
        self.store = store
        self.config = config
        self.max_meaning = int(config.get("max_meaning_chars", 200) or 200)
        self.extract_max = int(config.get("extract_max", 5) or 5)
        self.reject_words = parse_words(config.get("reject_words", ""))
        self.batch_size = max(3, int(config.get("learn_batch_size", 15) or 15))
        self.batch_delay_ms = max(0, int(config.get("learn_batch_delay_ms", 600) or 600))
        # 思考模型单批 15 条经常超过 2 分钟；低于 300 秒一律抬到 300
        self.batch_timeout = max(300, int(config.get("learn_batch_timeout", 300) or 300))
        # 学习时关思考的方式：auto/off/openai/glm/gemini/deepseek/custom
        self.think_mode = self._resolve_think_mode(config)
        self.extract_extra_body = self._parse_extra_body(
            config.get("extract_extra_body", ""))
        self.review_mode = self._resolve_review_mode(config)
        self.review_extra_body = self._parse_extra_body(
            config.get("review_extra_body", ""))
        self._no_think_blocked: set[str] = set()
        self._thinking_logged: set[str] = set()

    @staticmethod
    def _resolve_think_mode(config) -> str:
        mode = str(config.get("extract_thinking") or "").strip().lower()
        if mode:
            return mode
        # 旧版布尔配置
        return "auto" if bool(config.get("extract_no_think", True)) else "off"

    @staticmethod
    def _resolve_review_mode(config) -> str:
        # 默认保持思考，与提取分开配置
        return str(config.get("review_thinking") or "off").strip().lower()

    @staticmethod
    def _parse_extra_body(raw) -> dict:
        if isinstance(raw, dict):
            return raw
        if not raw:
            return {}
        try:
            value = json.loads(str(raw))
            return value if isinstance(value, dict) else {}
        except Exception:  # noqa: BLE001
            logger.warning("[slang] extract_extra_body 不是合法 JSON，忽略")
            return {}


    def _provider(self, umo):
        pid = str(self.config.get("provider_id") or "").strip()
        if pid:
            try:
                p = self.context.get_provider_by_id(pid)
                if p:
                    return p
                logger.warning(f"[slang] 配置的模型 {pid} 不存在，回退默认")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[slang] 取模型失败: {exc}")
        try:
            p = self.context.get_using_provider(umo)
            if p:
                return p
        except Exception:  # noqa: BLE001
            pass
        try:
            all_p = self.context.get_all_providers()
            return all_p[0] if all_p else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _provider_key(provider) -> str:
        cfg = getattr(provider, "provider_config", {}) or {}
        return str(cfg.get("id") or id(provider))

    def _thinking_extra(self, provider):
        """返回 (custom_extra_body, provider_config 顶层覆盖)。"""
        mode = self.think_mode
        if mode == "off":
            return {}, {}
        if mode == "custom":
            return dict(self.extract_extra_body), {}
        if mode == "glm":
            return {"thinking": {"type": "disabled"}}, {}
        if mode == "gemini":
            return {}, {"gm_thinking_config": {"budget": 0}}
        if mode == "deepseek":
            return {"reasoning_effort": "none"}, {}
        if mode == "openai":
            return {"reasoning_effort": "none"}, {}
        # auto：按 provider 标识猜
        cfg = getattr(provider, "provider_config", {}) or {}
        blob = " ".join(str(cfg.get(k, "")) for k in
                        ("id", "model", "provider_source_id", "type")).lower()
        if "glm" in blob or "zhipu" in blob:
            return {"thinking": {"type": "disabled"}}, {}
        if "gemini" in blob:
            return {}, {"gm_thinking_config": {"budget": 0}}
        return {"reasoning_effort": "none"}, {}

    def _no_think_provider(self, provider):
        """复制 provider，只改 custom_extra_body；不改全局配置、不建新提供商。"""
        mode = self.think_mode
        if mode == "off":
            return provider
        key = f"{self._provider_key(provider)}:{mode}"
        if key in self._no_think_blocked:
            return provider
        try:
            extra, updates = self._thinking_extra(provider)
            if not extra and not updates:
                return provider
            cfg = dict(getattr(provider, "provider_config", {}) or {})
            if extra:
                merged = dict(cfg.get("custom_extra_body") or {})
                merged.update(extra)
                cfg["custom_extra_body"] = merged
            for key, value in updates.items():
                cfg[key] = value
            clone = copy.copy(provider)
            clone.provider_config = cfg
            if mode not in self._thinking_logged:
                self._thinking_logged.add(mode)
                logger.info(
                    f"[slang] 学习关思考模式 {mode}: extra={extra}, cfg={updates}")
            return clone
        except Exception:  # noqa: BLE001
            return provider

    async def _call_provider(self, provider, prompt, timeout):
        coro = provider.text_chat(prompt=prompt)
        if timeout:
            return await asyncio.wait_for(coro, timeout=timeout)
        return await coro

    async def _chat(self, prompt: str, umo: str, timeout: int | None = None) -> str:
        provider = self._provider(umo)
        if not provider:
            logger.warning("[slang] 没有可用模型，跳过")
            return ""
        try:
            use = self._no_think_provider(provider)
            try:
                resp = await self._call_provider(use, prompt, timeout)
            except asyncio.TimeoutError:
                raise
            except Exception as exc:  # noqa: BLE001
                if use is provider:
                    raise
                self._no_think_blocked.add(
                    f"{self._provider_key(provider)}:{self.think_mode}")
                logger.info(
                    f"[slang] 该模型不支持关思考，回退普通请求：{exc}")
                resp = await self._call_provider(provider, prompt, timeout)
            return (resp.completion_text or "").strip()
        except asyncio.TimeoutError:
            logger.warning(f"[slang] 模型调用超时（{timeout}s），本批跳过")
            return ""
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[slang] 模型调用失败: {exc}")
            return ""

    @staticmethod
    def _merge_keywords(a: str, b: str) -> str:
        out = []
        for raw in (a, b):
            for x in re.split(r"[,，、;；\s]+", raw or ""):
                x = x.strip()
                if x and x not in out:
                    out.append(x)
        return ",".join(out)[:300]

    async def learn(self, umo: str, scope: str,
                    messages: list[dict]) -> int | None:
        """分批提取 + 合并候选 + 一次自审。

        返回入库条数；如果所有批次都失败（超时/解析失败）返回 None，
        让上层保留缓冲、进入冷却。
        """
        if not messages:
            return 0
        chat = "\n".join(f"{m['sender']}: {m['text']}" for m in messages)[-4000:]
        size = self.batch_size
        batches = [messages[i:i + size] for i in range(0, len(messages), size)]
        candidates: dict[str, dict] = {}
        failed = 0

        logger.info(f"[slang] {scope} 开始分批学习：{len(messages)} 条 / {len(batches)} 批")
        for idx, batch in enumerate(batches):
            text = "\n".join(f"{m['sender']}: {m['text']}" for m in batch)[-4000:]
            raw = await self._chat(
                EXTRACT_PROMPT.replace("{n}", str(self.extract_max))
                .replace("{chat}", text), umo, self.batch_timeout)
            items = parse_json(raw)
            if not isinstance(items, list):
                failed += 1
                logger.warning(f"[slang] {scope} 第 {idx + 1}/{len(batches)} 批解析失败")
            else:
                for it in items[: self.extract_max]:
                    if not isinstance(it, dict):
                        continue
                    name = _clean_name(it.get("name"))
                    meaning = _clean_meaning(it.get("meaning"), self.max_meaning)
                    if not name or not meaning:
                        continue
                    item = {
                        "name": name, "meaning": meaning,
                        "keywords": _clean_meaning(it.get("aliases"), 40),
                        "evidence": _clean_meaning(it.get("evidence"), 60),
                    }
                    cur = candidates.get(name)
                    if cur is None:
                        candidates[name] = item
                    else:
                        cur["keywords"] = self._merge_keywords(
                            cur["keywords"], item["keywords"])
                        if not cur["evidence"]:
                            cur["evidence"] = item["evidence"]
            if idx < len(batches) - 1 and self.batch_delay_ms:
                await asyncio.sleep(self.batch_delay_ms / 1000)

        if failed and failed == len(batches):
            logger.warning(f"[slang] {scope} 所有 {len(batches)} 批均失败，保留缓冲稍后重试")
            return None
        if not candidates:
            logger.info(f"[slang] {scope} 本轮没提取到条目")
            return 0

        cleaned = list(candidates.values())[: self.extract_max]
        kept = []
        for it in cleaned:
            if is_low_value(it["name"], it["meaning"]):
                logger.info(f"[slang] {scope} 过滤无信息量 ID 词条：{it['name']}")
                continue
            kept.append(it)
        cleaned = kept
        if not cleaned:
            return 0
        if self.reject_words:
            kept = []
            for it in cleaned:
                if it["name"].casefold() in self.reject_words:
                    logger.info(f"[slang] {scope} 命中拒绝词，丢弃：{it['name']}")
                    continue
                kept.append(it)
            cleaned = kept
            if not cleaned:
                return 0

        verdicts = await self._review(umo, cleaned)
        saved = 0
        for item in cleaned:
            certain, reason = verdicts.get(item["name"], (False, "未自审"))
            freq = self._freq(chat, item["name"])
            if freq < 2:
                certain, reason = False, f"仅出现 {freq} 次"
            elif certain and any(w in item["meaning"] for w in VAGUE_WORDS):
                certain, reason = False, "含义空泛（同义反复）"
            self.store.submit(
                item["name"], item["meaning"], scope=scope,
                aliases=item["keywords"], evidence=item["evidence"],
                auto_note=reason, approved=certain,
                contributor=self._contributor(messages, item["name"]),
            )
            saved += 1
            logger.info(
                f"[slang] {scope} {'直接入库' if certain else '进待审'}："
                f"{item['name']} = {item['meaning']}")
        logger.info(f"[slang] {scope} 分批学习完成：候选 {len(candidates)}，入库 {saved}，失败批 {failed}")
        return saved

    async def _review(self, umo: str, items: list[dict]) -> dict[str, tuple[bool, str]]:
        lines = "\n".join(f"- {it['name']}：{it['meaning']}" for it in items)
        old_mode, old_extra = self.think_mode, self.extract_extra_body
        self.think_mode, self.extract_extra_body = self.review_mode, self.review_extra_body
        try:
            raw = await self._chat(REVIEW_PROMPT.replace("{items}", lines), umo,
                                   self.batch_timeout)
        finally:
            self.think_mode, self.extract_extra_body = old_mode, old_extra
        data = parse_json(raw)
        out: dict[str, tuple[bool, str]] = {}
        if isinstance(data, list):
            for row in data:
                if not isinstance(row, dict):
                    continue
                name = _clean_name(row.get("name"))
                if not name:
                    continue
                out[name] = (bool(row.get("certain")), _clean_meaning(row.get("reason"), 30))
        if not out:
            logger.warning("[slang] 自审结果解析失败，全部进待复核")
        return out

    @staticmethod
    def _contributor(messages: list[dict], name: str) -> str:
        """找出最早提到这个词的发送者，作为来源群友。"""
        for msg in messages:
            if name and name in (msg.get("text") or ""):
                return str(msg.get("sender") or "")[:32]
        return ""

    @staticmethod
    def _freq(chat: str, name: str) -> int:
        """这条词在本批聊天里出现了几次。"""
        if not name:
            return 0
        return chat.count(name)
