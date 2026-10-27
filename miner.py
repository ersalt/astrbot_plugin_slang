# -*- coding: utf-8 -*-
"""提取 + 自审。LLM 调用通过 AstrBot provider，解析逻辑纯本地。"""

import json
import re

try:  # 允许脱离 AstrBot 单测
    from astrbot.api import logger
except Exception:  # noqa: BLE001
    import logging

    logger = logging.getLogger("slang")

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


class Miner:
    def __init__(self, context, store, config):
        self.context = context
        self.store = store
        self.config = config
        self.max_meaning = int(config.get("max_meaning_chars", 200) or 200)
        self.extract_max = int(config.get("extract_max", 5) or 5)

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

    async def _chat(self, prompt: str, umo: str) -> str:
        provider = self._provider(umo)
        if not provider:
            logger.warning("[slang] 没有可用模型，跳过")
            return ""
        try:
            resp = await provider.text_chat(prompt=prompt)
            return (resp.completion_text or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[slang] 模型调用失败: {exc}")
            return ""

    async def learn(self, umo: str, scope: str, messages: list[dict]) -> int:
        chat = "\n".join(f"{m['sender']}: {m['text']}" for m in messages)[-4000:]
        raw = await self._chat(
            EXTRACT_PROMPT.replace("{n}", str(self.extract_max)).replace("{chat}", chat), umo)
        items = parse_json(raw)
        if not isinstance(items, list) or not items:
            logger.info(f"[slang] {scope} 本轮没提取到条目")
            return 0

        cleaned = []
        for it in items[: self.extract_max]:
            if not isinstance(it, dict):
                continue
            name = _clean_name(it.get("name"))
            meaning = _clean_meaning(it.get("meaning"), self.max_meaning)
            if not name or not meaning:
                continue
            cleaned.append({
                "name": name,
                "meaning": meaning,
                "aliases": _clean_meaning(it.get("aliases"), 40),
                "evidence": _clean_meaning(it.get("evidence"), 60),
            })
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
            self.store.upsert(
                item["name"], item["meaning"], scope=scope, source="ai",
                aliases=item["aliases"], evidence=item["evidence"],
                auto_review=certain, auto_note=reason,
            )
            saved += 1
            logger.info(
                f"[slang] {scope} {'自审过关' if certain else '待复核'}："
                f"{item['name']} = {item['meaning']}")
        return saved

    async def _review(self, umo: str, items: list[dict]) -> dict[str, tuple[bool, str]]:
        lines = "\n".join(f"- {it['name']}：{it['meaning']}" for it in items)
        raw = await self._chat(REVIEW_PROMPT.replace("{items}", lines), umo)
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
    def _freq(chat: str, name: str) -> int:
        """这条词在本批聊天里出现了几次。"""
        if not name:
            return 0
        return chat.count(name)
