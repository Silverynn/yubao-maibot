# MaiBot 原生记忆与 Heart 插件：从聊天到“想起来”

适用范围：本仓库当前交付的 MaiBot、A_Memorix 与 Heart 插件。本文描述的是**这份实际安装版本**，不是所有版本的 MaiBot。下面提到的路径都相对于正在运行的 MaiBot 根目录；仓库中的 `extensions/` 是交付源码，安装器会把它复制到 MaiBot 的 `src/services/`。

## 先把几个概念分开

聊天记录像完整录像；长期记忆像从录像里摘出的卡片；人物画像像按某个人整理的卡片摘要；向量索引像按“意思相近”找卡片的目录；Heart 日志像记账本。日志中出现一句话不等于这句话已经进了长期记忆，检索返回一张卡片也不等于模型最终采纳了它。

原生主要有两类自动记忆：

1. **人物事实**：例如“某人正在学习 Python”。机器人回复后，后台尝试从该人的原始发言里提取稳定、与本人有关的事实。
2. **聊天摘要**：一个私聊或群聊累积到一定消息数，后台把这段会话整理成摘要。摘要是“大家讨论过什么”，不是“每个被提到的人都亲口确认过什么”。

## 一条人物事实怎么产生

`src/services/memory_flow_service.py` 的 `PersonFactWritebackService` 收到机器人**已生成的回复**后，将任务放进最多 256 项的后台队列。回复为空或极短的寒暄会跳过。它先定位目标人物，再收集同一会话中该人的原话及邻近上下文。群聊回复所引用的消息，只在当前群会话内查找。关键调用可概括为：

```python
facts = await self._extract_facts(target_person, reply_text, user_evidence_text)
for fact in facts:
    await store_person_memory_from_answer(..., evidence_message_ids=evidence_message_ids)
```

第一行让模型提出“可能值得记”的句子；后面逐条带上原始消息编号交给记忆服务。模型不是随便读到什么就记：`_extract_facts` 的原生提示词要求本人原话直接支持、关于本人、相对稳定，排除猜测、玩笑、机器人自己的推测和一次性安排。返回值必须是 JSON 数组；代码去空、去重、舍弃少于四个字符的片段，最多保留五条。模型调用失败也会返回空数组，因此空结果**可能是没有合格事实，也可能是提取失败**，不能只凭它认定用户的话没有价值。

Heart 的 `auto_candidates.selection_guidance` 会作为**附加筛选原则**接在原生提示词后面；不能推翻“必须有本人原话证据”这一原生规则。Heart 随后核实消息编号和发言者，并在同一会话寻找旧事实。如果没有可比较旧事实，默认让事实走原生写入；有旧事实才请模型检查冲突。不确定的进候选区，明确冲突的先私聊本人确认。此过程不能保证模型永不误判，所以仍需要审计和用户纠正。

## 群聊摘要怎么产生

同文件的 `ChatSummaryWritebackService._handle_message` 按会话数消息。此机当前原生设置 `chat_summary_writeback_message_threshold = 36`：自上次成功摘要后累计够 36 条才启动；它调用 `memory_service.ingest_summary`，带 `generate_from_chat=True` 和原会话 ID。A_Memorix 的 `summary_importer.py` 再取该会话最近的消息、组织摘要提示词、调用总结模型、解析结果，最后存进原生记忆。若生成失败，原生服务不会推进成功游标，下次仍可重试。**因此刚聊两句时 `/群回忆` 可能没有摘要，不代表插件坏了。**

## 真正存在哪里

`src/person_info/person_info.py` 的 `store_person_memory_from_answer` 和原生摘要流程最终走 `memory_service` → A_Memorix `ingest_text`。`src/A_memorix/core/runtime/services/ingest_service.py` 用 `external_id` 避免相同请求重复写入，保存正文和来源会话/人物/原始消息等元数据，再建立向量与相关索引，并安排人物画像刷新。它不是“只存在模型脑子里”。当前 `config/bot_config.toml` 的 `a_memorix.storage.data_dir` 是 `data/a-memorix`：其中 `metadata/metadata.db` 是 SQLite 正文与元数据，`vectors/` 下是语义检索所需的向量文件，另有图和索引文件。不要手动删改这些文件；只备份一个 SQLite 文件也未必能完整恢复索引状态。

Heart 的候选、确认请求和审计存在**另一套** `data/heart_observation/events.sqlite3`，易读日志在 `data/heart_observation/logs/QQ/` 和 `logs/Live2D/`；它们不是原生长期记忆库。日志按日期和会话分文件，按当前策略保留最近约三天。删长期记忆不会删除审计记录；这是“可用记忆”和“操作证据”的区别。

## 怎么“想起来”

原生 `src/A_memorix/core/runtime/services/memory_search_service.py` 允许按关键词、时间、混合与聚合等模式召回候选，再过滤已删/过期/不合当前会话的结果。Maisaka 可以调用 `query_memory` 或 `query_person_profile`；配置也可以自动把人物画像注入模型请求。当前运行配置中，人物画像注入和查询工具是开着的，启发式“按当前聊天印象自然召回”是关着的。即使某次日志显示“模型请求中的记忆参考”，也只说明参考材料送到了模型，不能证明回复采用了那条内容。

Heart 在**读取入口和返回出口**都核对当前群：私聊、别的群、混合来源和来源不明的记忆不得进本群。`/群回忆` 可看本群摘要；`/群回忆 @人` 只看本群与此人有关的已存事实，并把群聊摘要另标为“讨论提及”，不冒充本人事实。`/我的记忆` 与 `/忘记` 涉及个人内容，群里请求也只私发本人。

## Heart 究竟动了原生什么

原生 A_Memorix 的核心数据库算法、向量检索算法都**没有重写**。安装器应用的可回退补丁在 `patches/`，主要接入点如下：

| 原生接入文件 | 最小改动 | 原因 |
| --- | --- | --- |
| `src/services/memory_service.py` | 在统一 `_invoke` 入口加观察包装 | 写入前核查；读写后记录真实结果；群聊读写隔离 |
| `src/services/memory_flow_service.py` | 人物事实提取时追加可编辑筛选原则并记录结果；群聊 `reply_to` 按会话查；摘要带证据消息编号 | 不改原生提取算法与摘要算法，但能审计且避免跨群误归属 |
| `src/maisaka/memory/person_profile.py`、`src/maisaka/builtin_tool/query_person_profile.py` | 人物画像查询传入当前会话 ID | 避免群聊拿到同一人私聊的全局画像 |
| `src/maisaka/memory/heuristic_injector.py` | 启发式检索带真实会话 ID | 即使将来开启跨会话候选，也不能绕过群聊隐私边界 |
| `src/maisaka/builtin_tool/query_memory.py` | 给摘要加“不能冒充本人事实”的提示与日志识别结束标记 | 避免模型混淆摘要和事实，也使日志看得出参考内容 |
| `src/plugin_runtime/` 的少量接口 | 注册记忆确认、手动操作、候选和管理能力 | 让命令插件调用宿主原生服务，不直接碰数据库 |

实际逻辑在交付仓库的 `extensions/heart_host_observer.py`、`heart_memory_backend.py`、`heart_memory_scope.py` 和 `extensions/heart_shared/`。例如：

```python
result = await before_memory_write(component_name, args or {})
if result is None:
    result = await method(self, component_name, call_args, **kwargs)
```

第一行先问 Heart 是否需要暂缓/确认；返回 `None` 才调用原生写入。另一个关键检查 `visible(hit, current_chat_id, person_id)` 只让唯一来源于当前会话的记忆通过。因而 Heart 不是“第二个独立记忆库”，而是给原生记忆加门卫、确认台与记账本；候选区是临时等候区，不等于已经长期保存。

## 在 WebUI 自定义什么

进入 MaiBot WebUI → 插件 → Heart 记忆记录/管理 → 配置；保存后按 WebUI 提示重载或重启。已有配置文件不会被安装器覆盖。新版配置段如下：

| 配置段 | 字段 | 含义 |
| --- | --- | --- |
| 自动候选 `auto_candidates` | `selection_guidance` | 最重要：追加给原生 AI 的“什么值得长期记”原则，最多 2000 字 |
| 自动候选 | `auto_write_verified`、`max_pending_per_person` | 核实后自动写入还是先放候选；每人候选上限 |
| 冲突 `conflicts` | `candidate_limit`、`min_confidence`、`guidance`、`timeout_seconds`、`confirmation_minutes` | 最多比较多少旧事实、最低判断把握、补充判断原则、等待/确认时间 |
| 忘记 `forget` | `guidance`、`min_confidence`、`timeout_seconds`、`confirmation_minutes`、`list_page_size` | 自然语言定位原则、最低把握、超时、确认时间、`/我的记忆` 每页条数 |
| 群回忆 `group_recall` | `display_limit`、`include_group_summaries` | 最多显示多少条，以及是否展示本群摘要 |

提示词建议写**具体取舍**，例如：“优先记长期学习目标和稳定偏好；一次性的考试日期不要记；不确定就不提取”。不要写“记住所有内容”或“忽略原话证据”。冲突与遗忘的最低把握值在程序层还有 80% 安全下限，且删除和替换始终需要本人确认；用户配置不能关闭群聊隐私隔离。原生摘要的 36 条阈值、人物画像注入和启发式召回开关仍在 MaiBot 的 `config/bot_config.toml` `[a_memorix.integration]`，**不属于 Heart 插件设置**；首次调试不建议同时改许多原生开关。

## 如何验证，不把“看见日志”误作“已记住”

1. 用虚构测试人物发送 `/记住 测试同学长期喜欢围棋`，看回复和“记忆操作结果”是否都明确显示已存，再用 `/我的记忆` 检查。
2. 在**不同群**分别做 `/群回忆`；同一人的私聊事实不得出现在群内。群摘要必须标“本群摘要”。
3. 发送一条和旧事实相反的新事实；在本人私聊确认前旧事实应保留。取消或超时也不应变更。
4. 自然语言要求忘掉特定事实，先应看到待确认预览；确认后再用 `/我的记忆` 验证不在可用长期事实里。
5. 心情插件的日志同样按会话分文件；群聊消息数/最长等待会合并评估。模型超时、队列满时允许跳过情绪评估，不应捏造心情变化。

本次离线测试不接 QQ、不调用收费模型，也不能保证真实网络、模型或 NapCat 环境绝不会出现新问题。若真实回复仍有“说记住了但没写入”，请同时查原生写入结果、候选区和后台错误，而不是只看机器人措辞。
