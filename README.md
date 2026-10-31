# 群语汇 · astrbot_plugin_slang

给 AstrBot 用的**群黑话自动学习与注入**插件。让机器人自己从群聊里学会黑话，聊天时按关键词命中注入上下文，另外附带一个可选的梗百科词库。

为本地小模型（26B 级别）设计，重点是**不赌模型的自觉**：黑话靠关键词机械命中，不指望它记得去查。

> **隐私提示**：自动学习默认开启，插件会**在本机缓存群聊文本片段**；触发提取时，最近一段聊天会被发送给**你配置的模型服务**（本地模型不出本机，云端 API 则会发给服务商）。完整说明见 [PRIVACY.md](./PRIVACY.md)。

## 它做什么

- **自动学**：按群累计消息，达到阈值后交给 LLM 提取黑话候选
- **自己审**：模型判断这条能不能写成「X 是指 Y」，有把握的直接入库，没把握的进待审
- **人工管**：页面上审核、修改、删除、改归属，待审通过才参与注入
- **按群隔离**：同一个词在不同群可以有不同含义
- **命中注入**：每次请求前匹配关键词，命中的条目作为临时上下文追加到用户消息（不改 system prompt），最多 `inject_max` 条
- **梗百科兜底**：黑话没命中时，从梗Wiki 快照里补 1 条（可关）

## 工作流程

```
群消息 → 写入 buffer（按 umo 分组）
      → 攒够 learn_frequency 条
      → LLM 提取（最多 extract_max 条）
      → 模型自审（certain？）
          ├─ 是 → 直接进 auto（黑话库，可注入）
          └─ 否 → 进 auto_pending（待审，不可注入）
      → 每次 LLM 请求：内存关键词索引匹配已审词条（注入为临时上下文，不落库、不改 system prompt）
          ├─ 命中 → 注入（最多 inject_max 条）
          └─ 没命中 → 查 wiki（最多 1 条，带来源标注）
```

## 安装

1. 把整个 `astrbot_plugin_slang` 目录放进 `data/plugins/`
2. 重启 AstrBot（或在后台插件页重载）
3. 在插件管理里启用，按需改配置

依赖：无额外第三方包，只用 Python 标准库和 AstrBot 自带 API。

## 指令

前缀 `/yh`（语汇），全部仅管理员可用。

| 指令 | 作用 |
| --- | --- |
| `/yh list [页]` | 列已审词条（手动 + 自动），每页 8 条 |
| `/yh pending` | 看前 10 条待审 |
| `/yh search 关键词` | 按名称或含义搜 |
| `/yh add global\|group 名称 含义` | 手动录入 |
| `/yh approve 名称` | 待审 → 通过；已在库则驳回回待审 |
| `/yh del 名称` | 删除（词条或待审） |
| `/yh promote 名称` | 设为全局 |
| `/yh stat` | 统计 |
| `/yh check 一句话` | 预览这句话会注入什么 |

## 配置

后台插件配置页与面板「设置」同源（读 `_conf_schema.json`），共 18 项。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | true | 总开关：关掉后不学习也不注入 |
| `auto_learn` | true | 只关学习，注入照常 |
| `learn_frequency` | 50 | 每个群攒多少条消息触发一次提取（5–10000） |
| `learn_batch_size` | 15 | 分批学习每批消息数（3–40） |
| `learn_batch_delay_ms` | 600 | 批间等待毫秒（0–5000） |
| `learn_batch_timeout` | 120 | 单批提取超时秒数（30–600） |
| `extract_max` | 5 | 单次提取上限（1–100） |
| `inject_max` | 3 | 单次注入上限（1–50） |
| `max_meaning_chars` | 200 | 单条含义字数上限（20–1000） |
| `provider_id` | 空 | 提取用的模型，留空用当前会话模型 |
| `listen_private` | false | 是否也学私聊 |
| `blacklist_groups` | 空 | 不学习的群号，中英文逗号/顿号/分号/空格都认，保存统一成英文逗号 |
| `reject_words` | 空 | 拒绝词表：候选名称命中即丢弃，不送自审。分隔符同黑名单 |
| `filter_person_ids` | true | 过滤「群友 XXX 的 ID」这类同义反复的自动词条，保留别称 |
| `wiki_enable` | true | 启用梗百科；关掉后隐藏侧栏入口，也不参与注入 |
| `wiki_sync_enable` | false | 是否定时同步梗Wiki |
| `wiki_sync_interval_days` | 7 | 同步间隔（天，1–365） |
| `wiki_sync_time` | 04:30 | 每天几点检查，HH:MM |

## 管理页面

后台插件详情里打开本插件页面，左侧四个入口：

- **手动**：`manual` 表，人工录入，可注入
- **黑话库**：`auto` 表，模型学来，可注入
- **待审**：`manual_pending` / `auto_pending` 两张表的草稿，**永远不会被注入**
- **梗百科**：梗Wiki 快照，只读

父项可折叠；群名过长省略号；数字圆圈右对齐。

手动录入遇到同名同作用域的条目会拒绝覆盖，并提示已存在的是哪一条。关键词用于触发注入，不填则只用词条名。

词条字段：

| 字段 | 作用 |
| --- | --- |
| 词条名 | 展示名，默认也作为触发词 |
| 关键词 | 触发注入的词，逗号分隔；不填则只用词条名 |
| 内容 | 注入给模型的含义 |
| 其他 | 备注、出处、补充说明，只展示、不参与注入 |

每条三个按钮：

- **审核**：待审里 = 通过（手动草稿进手动，自动草稿进黑话库）；已审 = 驳回回对应待审表
- **修改**：改名称、关键词、含义、其他、归属（全局 / 某个群）
- **删除**：二次确认，直接删掉

待审列表支持勾选、全选本页、批量通过/删除。

「+ 手动录入」只在「手动」页显示，待审、黑话库、梗百科不出现，避免在非手动区录入却落到手动区。

所有操作结果统一用居中弹窗提示；保存失败会写清原因（如撞到哪个作用域的同名条目）。

词条 meta 会显示**来源**：自动学习记录最早提到该词的人，手动录入显示「手动录入」。

列表带分页；除梗百科外，可切换排序：默认「最新优先」（按更新时间倒序），也可选「次数优先」（按出现次数倒序）。

## 审核模型

四张物理表，按「来源 × 状态」拆分：

| 表 | 含义 | 参与注入 |
| --- | --- | --- |
| `manual` | 手动 · 已通过 | ✅ |
| `auto` | 自动 · 已通过 | ✅ |
| `manual_pending` | 手动 · 待审 | ❌ |
| `auto_pending` | 自动 · 待审 | ❌ |

- 学习中模型自审通过的进 `auto`，没把握的进 `auto_pending`；手动录入直接进 `manual`
- 匹配时用「词条名 + 关键词」；`其他` 不参与注入
- 学习按 `learn_batch_size` 分批提取，合并候选后统一自审，批间等待给前台让路
- 页面「审核」= 搬行：待审通过后进同来源的已审表，已审驳回后回同来源的待审表
- 操作按**自然键**定位：`(表, 名称, 作用域)`，id 只做内部主键
- 手动与自动可以同名同群共存；但审核搬行时目标表已有同名会拒绝，避免出现两条同名
- 重复提取只累加 `count`，不覆盖已写好的含义
- 注入时同名词条**手动优先**
- 启动时会把 1.x 的 `jargon` / `pending` 自动拆进四张表，备份为 `jargon.db.pre-four-tables.<时间>.bak`

## 梗百科

词条来自 [cnmeme.wiki](https://cnmeme.wiki) 的公开只读 API（[文档](https://cnmeme.wiki/developers/api)），不需要 Key。

- 手动同步：`python tools/sync_memes.py --db <数据目录>/jargon.db`，加 `--full` 全量重拉
- 自动同步：设置里打开后，每天到点检查，距上次超过间隔天数才拉增量
- 限速：0.5 秒/页，429 遵循 `Retry-After`
- 只拉列表接口（自带摘要），6668 条约 223 次请求，不逐条抓详情
- 注入时带「梗Wiki」来源标注

关掉 `wiki_enable` 后，侧栏入口、注入、相关设置一起隐藏。

## 数据库

SQLite，默认在 `data/plugin_data/astrbot_plugin_slang/jargon.db`，WAL 模式。

| 表 | 内容 |
| --- | --- |
| `manual` | 手动已审：name / keywords / meaning / extra / scope / count / auto_note / evidence / contributor |
| `auto` | 自动已审，字段同上 |
| `manual_pending` | 手动待审，字段同上 |
| `auto_pending` | 自动待审，字段同上 |
| `wiki` | 梗快照：title / tags / summary / updated_at |
| `buffer` | 待学消息，按 umo 分组，启动清 7 天前，超过 2 万行自动截断 |
| `meta` | schema_version、wiki_synced_at |

四张词条表都有 `UNIQUE(name, scope)` 与 scope/updated/name 索引。

### 代码分层

```
main.py       AstrBot 接入：指令、事件、Web API（只解析参数）
service.py    业务规则：审核方向、冲突检查、注入索引、分页合并
repo（store） 通用单表 CRUD，表名白名单，四张表复用同一实现
index.py      内存关键词索引（首字符分桶），注入不再全表读取
tests/        服务层回归测试
```

运行测试：

```bash
# 在插件目录下
python -m unittest discover tests -v
```

## 已知限制

- 关键词是子串匹配，半截词要靠 `aliases` 补
- 提取质量取决于模型，26B 偶尔把普通词当黑话——所以有待审表；可用 `reject_words` 硬过滤掉固定不收的词
- 模型自审可能误放行；不放心就把 `auto_learn` 关掉，只手动录入
- 每条含义最多 `max_meaning_chars` 字、单次最多 `inject_max` 条
- 梗库只存摘要，没有正文和「怎么接」，需要更详细的用法得另做

## 来源与致谢

具体边界见 [NOTICE.md](./NOTICE.md)。

| 项目 | 许可 | 我们用了什么 |
| --- | --- | --- |
| [MaiBot](https://github.com/Mai-with-u/MaiBot) | 自定义 EULA（非开源许可） | 只借鉴「自动学黑话再注入」的思路，未使用代码 |
| [astrbot_plugin_jargon](https://github.com/sleep430/astrbot_plugin_jargon) | 仓库无 LICENSE（保留所有权利） | 只借鉴流程与字段设计，未使用代码 |
| [astrbot_plugin_memewiki](https://github.com/in1t1a1/astrbot_plugin_memewiki) | MIT | 参考 cnmeme.wiki 公开 API 的同步方式，独立实现 |

梗词条来自 cnmeme.wiki，版权归站点及原作者；本项目不打包词库，只本地缓存，注入时保留来源标注。

## 版本状态

| 版本 | 状态 |
| --- | --- |
| `v1.4.10` 及以后 | **推荐使用** |
| `v1.4.0`–`v1.4.9` | 已废弃：群消息被误判为私聊，注入与自动学习静默失效（`v1.4.5` 是唯一发出去的正式版） |
| `v1.1.0`–`v1.3.1` | 已废弃：四表重构前的旧架构，存在严重 UI 问题（编辑/审核定位错乱、页面状态不同步等） |
| `v1.0.x` | 历史版本，可用但不建议 |

旧版本的 GitHub Release 标题已标「⚠️ 已废弃」，tag 保留以便回溯。

## 版本规则

- **主版本**（x）：不兼容改动，如数据结构、接口、目录变更
- **次版本**（y）：新增功能
- **修订**（z）：修 bug、文案、样式，不改变用法

例子：加拒绝词表是 `1.1.0`，修冲突提示是 `1.4.1`。

## 更新日志

版本变更见 [CHANGELOG.md](./CHANGELOG.md)。

## 路线图

待更新项见 [TODO.md](./TODO.md)。

## 许可

[MIT](./LICENSE) © 2026 贰叔

---

Power By DeepSeek V4.1 Flash 1017
