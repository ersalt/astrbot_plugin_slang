# astrbot_plugin_slang

群黑话学习与注入。为本地 26B 小模型写的轻量版，重点是**不赌模型的自觉**。

## 怎么装

1. 把整个 `astrbot_plugin_slang` 文件夹复制到
   `<AstrBot 实例>/core/data/plugins/` 下；
2. 在后台插件页启用它，重载一次；
3. 首次启动会自动把同目录的 `seed.txt` 导入为手动词条（仅当库为空时）。

同时去后台**停用旧的 `astrbot_plugin_jargon`**，两套学习逻辑不要同时跑。

## 工作方式

```
群消息 → 落 SQLite 缓冲 → 攒够 learn_frequency 条
      → LLM 提取黑话 → LLM 自审（确定/推测）
      → 入库并打标记（自审过关 / 待复核）
回复时 → 拿用户这句话做关键词匹配 → 最多 3 条拼进 system_prompt
```

三个关键取舍：

- **缓冲和计数都在 SQLite 里**，重启、热重载都不丢。旧版把计数放内存，改个配置就归零。
- **注入不调模型**，纯字符串匹配，毫秒级、零成本，不会出现“模型该查却没查”。
- **提取结果不一定可信**，所以先自审再入库，但自审不过关的只是不注入，不会删，留给你人工复核。

## 管理页面

装好后在后台插件详情里打开本插件页面（`pages/kb/index.html`），左边是三级导航，右边是条目：

- **手动黑话**：下分「全局」和各个群，看的是来源为手动录入的词条
- **群黑话**：按群分组，看的是自动学到的词条
- **待审黑话**：还没人工通过的全部条目，同样按群分组

每条是一个气泡，左边词条名、中间含义、右边 2×2 四个按钮：自审 通过/不通过、人工 通过/不通过、修改、删除。点「修改」弹窗里可以改词条名、内容、归属（全局或某个群）。

右上角搜索框按 `/` 聚焦，`Esc` 关弹窗。左侧底部有手动录入和设置。

## 指令

| 指令 | 作用 |
| --- | --- |
| `/kb list [页]` | 列出词条，带标记 |
| `/kb pending` | 只看待复核 |
| `/kb search 关键词` | 按名字/含义搜 |
| `/kb add global 名称 含义` | 手动录入，全局 |
| `/kb add group 名称 含义` | 手动录入，只在当前群生效 |
| `/kb approve 名称` | 标记人工过关 |
| `/kb promote 名称` | 群词条提升为全局 |
| `/kb del 名称` | 删除 |
| `/kb stat` | 统计 |
| `/kb check 一句话` | 试一下这句话会注入什么 |

名称也可以填 `#12` 这种 id。同名条目分属不同群时，用 id 更准。

## 标记含义

```
人工✅  你确认过的（/kb approve 或 /kb add）
自审✅  模型自审认为“能写成 X 是指 Y”
自审✗   自审认为含义含糊，只进待复核，默认不注入
```

默认只注入 `人工✅` 或 `自审✅`；想让待复核的也参与，把 `inject_pending` 打开（不建议）。

## 配置

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | true | 总开关 |
| `auto_learn` | true | 关掉就只做人工维护 |
| `learn_frequency` | 50 | 每群攒多少条触发一次 |
| `extract_max` | 5 | 单次最多提取几条 |
| `inject_max` | 3 | 单次最多注入几条 |
| `max_meaning_chars` | 200 | 单条含义字数上限 |
| `inject_pending` | false | 待复核是否也注入 |
| `provider_id` | 空 | 提取用哪个模型，留空用会话模型 |
| `listen_private` | false | 是否学私聊 |
| `blacklist_groups` | 空 | 不学的群号 |

## 数据库

`data/plugin_data/astrbot_plugin_slang/jargon.db`

- `jargon`：`name + scope` 唯一。`scope` 为空表示全局，否则是群号；`source` 区分 `manual` / `ai`；`review_auto` / `review_manual` 两个审核标记。
- `buffer`：待学习的原始消息，按 `umo` 分组，触发后清空；启动时清理 7 天前的残留。
- `meta`：存 `schema_version`。

首次启动会自动把 v1（带 `category` 字段的旧表）迁移到 v2，迁移前把原库备份成 `jargon.db.v1.bak`。同名同作用域的重复条目会合并：手动条目优先，次数相加。

想看内容直接：

```sql
SELECT id,name,meaning,scope,source,count,review_auto,review_manual FROM jargon ORDER BY id DESC;
```

## 已知限制

- 关键词是子串匹配，`文锋` 这种半截词需要在 `aliases` 里补；
- 自审质量取决于提取用的模型，26B 偶尔会把普通词当黑话，这就是待复核队列存在的意义；
- 每条含义最多 200 字、单次最多 3 条，是给小模型定的硬上限，嫌少自己调配置。

## 梗库（梗Wiki 公开 API）

黑话没命中时，从 `wiki` 表兜底注入 1 条梗，只给一句用法，结尾带来源「梗Wiki」。

数据来自 [梗Wiki 开发者 API](https://cnmeme.wiki/developers/api)：只读公开词条，**无需登录或 API Key**。

```bash
python tools/sync_memes.py                # 增量（按 updated_at 水位）
python tools/sync_memes.py --full         # 全量重拉
python tools/sync_memes.py --max-pages 2  # 试水
```

接口约束（照文档执行）：

- `limit` 最大 30，按更新时间倒序，用 `has_more` 翻页
- 搜索缓存 60 秒、详情 120 秒，支持 ETag / 304
- 429 要遵守 `Retry-After`；文档明确要求「限制并发、不要密集轮询」，脚本默认 0.5 秒一页
- 内容使用时保留来源

`wiki` 表只存 `title` / `tags` / `summary` / `usage` / `origin` / `updated_at`。匹配**只认标题**，不认 tags——tags 里全是「表情包」「天天」这类分类词，拿去匹配会疯狂误命中。
