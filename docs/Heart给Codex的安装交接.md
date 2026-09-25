# 给同伴及 Codex 的 Heart 安装交接

本次交付：记忆审计/候选/忘记/冲突确认 1.2.0、心情状态 1.3.0。仓库保存可维护源码和原生接入补丁，不保存完整运行实例。仅拉取代码不会自动改变正在运行的机器人。记忆新指令与WebUI设置请读《Heart记忆升级1.2使用说明.md》。

## 可以直接发给 AI 助手的任务

请在这个仓库帮我安装 Heart 记忆与心情扩展。先读 README.md、docs/Heart伙伴安装与使用教程.md、docs/Heart底层改动与文件清单.md 和目标目录适用的 AGENTS.md，再行动。

1. 先检查 Git 分支、工作区改动与实际 MaiBot 路径。确认已取得本次交接分支或合并后的代码；不要覆盖我的未提交修改，不要执行 hard reset。机器人目录应包含 bot.py 与 src/services/memory_service.py，外层合作仓库并不是运行目录。
2. 如果尚未准备运行环境，遵循 README 的固定版本 bootstrap 流程，只在全新目录部署，不删除已有 .runtime。若使用已有机器人，先检查版本与补丁兼容性。不要为了通过检查自动升级上游或改用最新版。
3. 在改动运行目录前确认机器人已关闭，备份插件配置、下述四个原生文件和已有 Heart 数据。保存备份于本机，不提交到 GitHub。不要停止其他无关进程。
4. 在仓库根目录执行 `python scripts/install_heart_plugins.py --target "实际MaiBot路径"`。这是只检查。若报告接口冲突或不认识的修改，停止并展示具体差异，让我决定如何处理；不要强行覆盖或跳过校验。
5. 预检通过后，执行相同命令并加 `--apply`。它部署两个插件、共用模块、宿主适配模块和五份补丁，保留已有 config.toml。安装器不是跨文件事务，失败时应检查实际改动后恢复，不要假定全部回滚。
6. 使用目标 MaiBot 的 Python 环境，在外层仓库运行 `python -m unittest discover -s tests -p "test_heart*.py"`（将 python 替换为该环境解释器）。当前基线是 57 项离线测试。该测试使用虚构模型结果，不调用付费模型、不发 QQ 消息。`verify_heart_integration.py` 会加载目标插件，只能在隔离测试实例执行，不能未经检查在正式实例执行。
7. 再运行安装器只检查，确认无待更新文件和补丁。说明修改了哪些文件、测试结果及未验证事项。不要直接改我的模型密钥、QQ 身份、人设、群白名单或开机启动设置。
8. 指导我在 WebUI 配置插件。AI 心情默认关闭；开启前说明会调用 utils 模型并可能计费，先征得我的同意。检查 ai.timeout_seconds（建议从20秒开始）、max_output_tokens（默认4096）、delta_multiplier、positive_limit、negative_limit、mood.max_delta 与冷却时间。旧配置不会自动替换为新版默认值。
9. 指导我设置 states 分数区间和对应风格。保持0—100完整覆盖、不重叠；风格只影响表达，不能降低帮助质量、修改事实或让机器人责怪用户。说明 AI 评估异步执行，可能影响下一次回复而非本次。
10. 联网启动、真实模型调用和 QQ 发消息前再次征得同意。先使用测试账号私聊验收日志、心情和记忆冲突确认；不要使用真实敏感信息测试。

## 必须完整部署的内容

- `plugins/heart_memory_audit/`、`plugins/heart_mood/`。
- `extensions/heart_shared/`：部署至运行根目录的 `heart_shared/`。
- `extensions/heart_host_observer.py`、`extensions/heart_memory_backend.py`：部署至运行目录 `src/services/`。
- `patches/heart-memory-observer.patch`、`patches/heart-memory-confirmation.patch`、`patches/heart-memory-entry.patch`、`patches/heart-memory-prompt.patch`、`patches/heart-memory-decision-log.patch`：由安装器应用。
- 原生接入修改涉及 `src/services/memory_service.py`、`src/services/memory_flow_service.py`、`src/plugin_runtime/hook_catalog.py`、`src/plugin_runtime/capabilities/registry.py`。不要拿文档中的示意代码覆盖原生文件。

## 验收标准与边界

- 日志位于实际 MaiBot 的 `data/heart_observation/logs/YYYY-MM-DD.txt`，当前心情摘要位于 `data/heart_observation/当前心情.txt`。检查真实对话、记忆操作和心情变化原因，不把模型口头说“记住了”作为存储成功证据。
- 原生记忆仍由 MaiBot 管理。日志区分检索候选、提供给模型的参考和实际存储结果；提供参考不等于模型一定采用。
- 冲突确认适用于经过受支持入口、归属明确的个人事实，不保证拦截所有类型的记忆写入。私聊按提示执行 `/确认记忆更新 编号`，取消或超时不应修改旧事实；不得绕过原用户身份验证。
- 测试新旧事实时应检查原生写入及旧事实状态；跨存储更新非原子事务，部分失败不能报作全部成功。
- 审计日志包含对话原文，属于隐私数据。原生“忘记”不会自动删除日志副本。不要提交日志、数据库、API密钥、登录态或带真实账号的运行配置。
- 回退先停机、关闭插件并备份；按底层改动文档反向检查补丁。不要删除记忆数据或覆盖整个 src。

## 建议验收顺序

离线测试 → 安装预检 → 部署 → 二次预检 → 用户授权后启动 → 一条普通消息日志 → 一条感谢消息的 AI 评估及数值变化 → 自定义风格区间 → 个人事实冲突的取消和确认各一次。

验收记录写清“已测试 / 未测试 / 失败原因”，不要用“所有功能已完成”代替证据。
