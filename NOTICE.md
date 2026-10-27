# 来源与致谢

本项目（**群语汇 / astrbot_plugin_slang**）由 **贰叔** 编写，以 [MIT](./LICENSE) 许可发布。

功能上参考了下面几个项目。为尊重版权，这里写清楚**参考了什么、有没有用代码、对方是什么许可**。

---

## 1. MaiBot —— 思路来源，未使用代码

- 仓库：https://github.com/Mai-with-u/MaiBot
- 参考内容：从群聊记录中自动学习黑话、再在对话时注入的**整体思路**。
- 许可情况：仓库使用自定义的最终用户许可协议（`EULA.md`），不是 OSI 认可的开源许可。
- 本项目声明：**没有复制 MaiBot 的任何代码**，也没有包含其资源文件；仅借鉴产品思路，按独立实现编写。

## 2. astrbot_plugin_jargon —— 思路与字段设计参考，未使用代码

- 仓库：https://github.com/sleep430/astrbot_plugin_jargon
- 作者：月咏小眠 & 睡觉
- 参考内容：阶梯阈值触发提取、命中后机械匹配注入的流程，以及词条字段（名称 / 别名 / 含义 / 作用域 / 计数）的设计。
- 许可情况：该仓库**未附带 LICENSE 文件**。按著作权法默认规则，未声明许可即保留所有权利，因此不能复用其代码。
- 本项目声明：**没有复制其代码**。数据库结构、提取提示词、匹配与注入逻辑均为独立编写。

## 3. astrbot_plugin_memewiki —— 数据获取方式参考（MIT）

- 仓库：https://github.com/in1t1a1/astrbot_plugin_memewiki
- 作者：in1t1a1
- 许可：MIT，Copyright (c) 2026 in1t1a1
- 参考内容：通过 cnmeme.wiki 公开只读 API 同步梗词条的方式（分页、限速、429 退避、增量水位）。本项目的 `wikinow.py` 为独立实现，未包含其代码或内置数据库。
- MIT 全文见文末。

---

## 数据来源与使用约束

梗词条内容来自 **[cnmeme.wiki](https://cnmeme.wiki)** 的公开只读 API（[开发者文档](https://cnmeme.wiki/developers/api)）：

- 接口：`GET https://cnmeme.wiki/api/v1/public/memes`
- 词条版权归 cnmeme.wiki 及原作者所有。
- 本项目只做**本地缓存与检索**，不打包、不再分发词条数据库；注入时保留「梗Wiki」来源标注。
- 遵守站点规则：`limit` ≤ 30、按更新时间倒序翻页、429 时遵循 `Retry-After`、限制并发、不做密集轮询。同步默认 0.5 秒/页。
- 站点 `robots.txt` 声明 `search=yes, ai-train=no, use=reference`；本项目仅作检索参考注入，不用于模型训练。

如果你要二次分发本项目，请自行确认上述内容的适用授权。

---

## MIT 许可全文（astrbot_plugin_memewiki）

```
MIT License

Copyright (c) 2026 in1t1a1

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
