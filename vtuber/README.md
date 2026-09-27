# 鱼宝 VTuber 配置与本地接入

这份目录是正在运行的 Open-LLM-VTuber 定制快照，基于上游 `Open-LLM-VTuber/Open-LLM-VTuber` 提交 `992309c0aa19845960228f880013d4685fde93b5`。`frontend/` 是上游独立子模块，当前本地版本为 `06a659b114fff788cf0daaa86e484576db4975bf`。本目录不是完整的上游程序；先取得这两个上游版本，再放入这里的定制文件。

## 分享了什么

- `conf.example.yaml`：从本地运行配置导出的示例，已把 13 处密钥字段替换为 `SET_LOCALLY`。复制成自己电脑上的 `conf.yaml` 后再填自己的凭据。不要上传填好密钥的文件。
- `model_dict.json`、`live2d-models/ds-whale-girl/`：鱼宝 Live2D 角色定义和模型。模型目录内的 `LICENSE`、`README.md` 必须随模型保留。
- `maibot_client.py`、`src/`：连接 MaiBot WebUI、过滤富媒体朗读、表情判断和 Fish Audio 语音等定制。`maibot_client.py` 目前按“Open-LLM-VTuber 与 qq机器人并列”来找本机 `webui.json`；不同电脑应核对这两个文件夹的位置。
- `frontend/yubao-expression-timing.mjs`、`scripts/apply_yubao_expression_timing.py`：让短暂表情自动回到“平静”。将这些文件放到上游对应路径后，在 Open-LLM-VTuber 根目录运行 `python scripts/apply_yubao_expression_timing.py`。脚本检查当前前端编译文件，版本变化时会停止，不能强行替换。
- `scripts/test_agent_changes.py`、`scripts/test_expression_timing.mjs`：不发送 QQ 消息的离线检查。

## 在自己的电脑恢复

1. 单独下载上述固定版本的 Open-LLM-VTuber 和 frontend 子模块，安装其 Python 依赖。不要直接覆盖已有工作副本；先比较修改。
2. 按本目录的相对路径把定制文件复制到 Open-LLM-VTuber 根目录。`conf.example.yaml` 是示例，不是实际运行的 `conf.yaml`；自己复制并填入自己的密钥。
3. 确认 MaiBot WebUI 的 `8001` 端口已运行，核对 `maibot_client.py` 查找本机 `webui.json` 的路径。浏览器使用 VTuber 的 `12393` 端口。
4. 运行前端表情补丁脚本，再按上游方式启动程序。修改前端后在浏览器按 Ctrl+F5。

`.env`、真实 `conf.yaml`、聊天记录、日志、缓存、虚拟环境、MaiBot 的记忆数据库和 QQ 登录态都没有收录。当前使用的教室背景图也未收录，因为这里没有核实到它的可再发布许可。新电脑要自行选一张有权使用的背景。

Live2D 模型的美术许可为 **CC BY-NC-SA 4.0**：署名、非商业使用、改编后以相同许可分享；详见模型目录 `LICENSE`。第三方代码与模型分别遵守各自的许可证。
