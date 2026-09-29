# 鱼宝 VTuber 配置与本地接入

这份目录是正在运行的 Open-LLM-VTuber 定制快照，基于上游 `Open-LLM-VTuber/Open-LLM-VTuber` 提交 `992309c0aa19845960228f880013d4685fde93b5`。`frontend/` 是上游独立子模块，当前本地版本为 `06a659b114fff788cf0daaa86e484576db4975bf`。本目录不是完整的上游程序；先取得这两个上游版本，再放入这里的定制文件。

## 分享了什么

2026-09-28 新增 Heart 1.4 持久心情面板、按话题结束的临时情绪、表情回执和 QQ/Live2D 分目录日志，见 [Heart话题情绪与Live2D接入](../docs/Heart话题情绪与Live2D接入.md)。启用 `HEART_MAIBOT_ROOT` 后，由 Heart 统一控制表情，下面的旧3秒恢复和独立分类不再接管它。

2026-09-27 的文字模式表情修复、测试方法和表情包兼容性说明，见 [表情修复与测试说明](表情修复与测试说明.md)。

2026-09-29 修复重进网页时“聊天框为空但 MaiBot 暗中延续旧对话”：每个新的 Live2D 网页连接使用独立的 MaiBot 身份，旧网页的临时情绪结束并记日志；长久保留的 QQ 私聊/群聊、记忆库均不会被删除。当前策略是新网页不继承旧网页的个人记忆；若需要跨网页保留经确认的长期事实，应另做显式的长期身份映射，不要把旧聊天上下文偷偷带回。详见 [重进会话与模型错误说明](../docs/Live2D重进会话与模型错误说明.md)。

- `conf.example.yaml`：从本地运行配置导出的示例，已把 13 处密钥字段替换为 `SET_LOCALLY`。复制成自己电脑上的 `conf.yaml` 后再填自己的凭据。不要上传填好密钥的文件。
- `model_dict.json`、`live2d-models/ds-whale-girl/`：鱼宝 Live2D 角色定义和模型。模型目录内的 `LICENSE`、`README.md` 必须随模型保留。
- `maibot_client.py`、`src/`：连接 MaiBot WebUI、过滤富媒体朗读、表情判断和 Fish Audio 语音等定制。`maibot_client.py` 目前按“Open-LLM-VTuber 与 qq机器人并列”来找本机 `webui.json`；不同电脑应核对这两个文件夹的位置。
- `frontend/yubao-expression-timing.mjs`、`scripts/apply_yubao_expression_timing.py`：修复无声模式的表情和字幕，让短暂表情保留约 3 秒再回到“平静”，避免对话结束时立即清除。将这些文件放到上游对应路径后，在 Open-LLM-VTuber 根目录运行 `python scripts/apply_yubao_expression_timing.py`。脚本检查当前前端编译文件，版本变化时会停止，不能强行替换。
- `scripts/test_agent_changes.py`、`scripts/test_expression_timing.mjs`：不发送 QQ 消息的离线检查。

## 在自己的电脑恢复

1. 单独下载上述固定版本的 Open-LLM-VTuber 和 frontend 子模块，安装其 Python 依赖。不要直接覆盖已有工作副本；先比较修改。
2. 按本目录的相对路径把定制文件复制到 Open-LLM-VTuber 根目录。`conf.example.yaml` 是示例，不是实际运行的 `conf.yaml`；自己复制并填入自己的密钥。
3. 确认 MaiBot WebUI 的 `8001` 端口已运行，核对 `maibot_client.py` 查找本机 `webui.json` 的路径。浏览器使用 VTuber 的 `12393` 端口。
4. 运行前端表情补丁脚本，再按上游方式启动程序。修改前端后在浏览器按 Ctrl+F5。

`.env`、真实 `conf.yaml`、聊天记录、日志、缓存、虚拟环境、MaiBot 的记忆数据库和 QQ 登录态都没有收录。当前使用的教室背景图也未收录，因为这里没有核实到它的可再发布许可。新电脑要自行选一张有权使用的背景。

Live2D 模型的美术许可为 **CC BY-NC-SA 4.0**：署名、非商业使用、改编后以相同许可分享；详见模型目录 `LICENSE`。第三方代码与模型分别遵守各自的许可证。
