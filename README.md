# 极创二面题heart项目

基于 **MaiBot + NapCatQQ** 的 QQ 角色机器人定制。用于学习和完成 [Heart Heart Heart 项目](https://join.geek-tech.club/problems2/heart-heart-heart)：理解对话、状态、记忆与工具执行，并逐步加入虚拟形象和实时语音。

这里共享的是现有定制、可重建补丁和学习计划，不是从零编写的聊天框架，也不表示题目的全部功能已完成。

## 已有改动

- 按固定账号区分管理员与普通成员，约束人物画像和事实归属。
- 私聊、艾特、名字提及及戳一戳的回复触发；戳一戳回声过滤。
- 按语境附表情、表情单独发送、群聊文字与表情 +1、黑话参考。
- 回复分句保留必要标点，调整口语风格。
- 现实通知必须有原始成员消息依据；摘要排除机器人发言和旧摘要递归引用。
- Windows 中文路径下的 FAISS 索引读写兼容。
- 启动守护、登录自启、持久化补丁；模型选择由 WebUI 维护。
- 修复回复初始化块重复插入，增加隔离回归测试。

**仍需完成或验证：** 普通回复与强制队列的硬性去重、延迟与过时消息、切分超限退化、提醒送达确认、记忆修改和忘记闭环、可观察角色状态、虚拟形象、连续语音和打断。语音插件已安装过不等于实时语音已实现。

## Heart 记忆与心情扩展（新增交接）

已提供记忆审计/冲突确认 **1.1.0**、心情状态 **1.3.0** 的源码、默认配置、最小原生补丁及安装器。它们补全原生记忆的观察与确认流程，不替代原生记忆系统；心情支持关键词或可开关的 AI 评估、自定义分数区间与风格。

- 人工安装：[安装与使用教程](docs/Heart伙伴安装与使用教程.md)。
- 让 Codex 协助安装：[可直接交给 AI 的交接说明](docs/Heart给Codex的安装交接.md)。
- 合并审阅：[底层改动清单](docs/Heart底层改动与文件清单.md)、[输出预算故障复盘](docs/Heart400输出预算故障复盘.md)。

**拉取仓库不等于已安装到运行实例。** 在基础环境准备好、目标机器人停机并备份后，先运行 `python scripts/install_heart_plugins.py --target "实际MaiBot目录"` 检查，通过后加 `--apply` 部署。只复制两个插件目录会漏掉必要接口。AI 心情默认关闭，模型调用可能计费；真实 QQ 联机需自行授权与验收。

当前扩展通过51项离线测试；并不表示所有记忆路径都具备冲突拦截、审计副本随“忘记”自动删除，或 Lv3/Lv4 已完成。完整边界见教程。

## 源码目录

| 路径 | 内容 |
|---|---|
| `upstreams.json` | MaiBot 和四个插件的仓库地址、固定提交、补丁及许可证位置 |
| `patches/` | MaiBot、NapCat Adapter、戳一戳插件的当前代码差异，包含依赖锁文件改动 |
| `scripts/` | 持久化补丁、Windows 启动脚本、补丁测试、上游获取脚本 |
| `config/personality.md` | 历史人设源文件，已脱敏；与实际运行配置有差异，不自动认定完全一致 |
| `config/examples/` | 主配置和插件配置的脱敏快照 |
| `docs/` | 32 项任务计划、现状盘点、历史方案、协作说明 |
| `tools/historical/` | 历史模型探测脚本，仅供研究，不在安装或测试中自动执行 |
| `licenses/` | 上游提供的许可证原文 |

## 同伴第一次取用（Windows / PowerShell）

准备 Git、Python 3.12 和 uv。NapCatQQ Desktop 需要单独安装并由你自行登录 QQ；本项目不携带 QQ 登录态。

```powershell
git clone https://github.com/Silverynn/yubao-maibot.git
cd yubao-maibot
python scripts/bootstrap.py
python scripts/bootstrap.py --apply
```

第一条 bootstrap 命令只显示计划；`--apply` 下载 `upstreams.json` 中的固定版本并应用补丁，不启动机器人。只在全新的项目副本使用；发现目标目录已存在会停止，避免覆盖他人的工作。下载中断后应先检查已有目录，不要为重新运行而删除有修改的目录。

安装 MaiBot 依赖：

```powershell
cd .runtime/MaiBot
uv sync
cd ../..
```

插件的额外依赖及 NapCat 连接方式请查看固定版本各自的 README。完整 QQ 联机、模型服务与语音依赖尚未在另一台电脑验证，不能承诺仅运行以上命令就能上线。

### 配置自己的测试实例

1. 将 `config/examples/config/` 中两个 TOML 文件复制到 `.runtime/MaiBot/config/`。对已有实例先比较和备份，不能直接覆盖。
2. 将 `config/examples/plugins/插件目录/config.toml` 复制到对应运行插件目录。Adapter 示例默认禁用；准备好测试账号与群白名单后才启用。
3. 在本机运行配置填入自己的模型 API 密钥，按账号实际可用的模型核对任务分配。示例中的模型名称只是原实例快照，不保证你的账号或未来服务仍支持。
4. 替换身份占位值：`9000000001` 为管理员、`9000000002` 为示例朋友、`9000000003` 为机器人；其他 `90000001xx` 数字为脱敏占位值。`AdminAlias`、`admin_alias`、`friend_example` 为示例别名。
5. 身份目前仍散落在代码补丁、持久化脚本、人设和运行配置里。修改时必须保持一致，重点检查 `scripts/apply_response_guarantees.py`、`config/personality.md`、运行 TOML 以及已应用补丁的 Python 文件。后续任务应将身份集中配置；不能只改人设里的昵称。
6. 检查 `scripts/apply_response_guarantees.py` 内的群白名单占位值。它会在启动时维护相关配置，不要保留示例群号后误以为已经接入自己的群。
7. 空 embedding 列表是当前示例配置；记忆有稀疏检索回退，不代表 embedding 已接好。不要把聊天模型直接充当向量模型。

主配置保留了历史人设与事实约束，部分叙述用于当时实例。移植时应自行审阅；它不是其他人的真实身份、经历或通知。

### 验证与启动

先运行不联网、不发消息的补丁测试：

```powershell
.runtime/MaiBot/.venv/Scripts/python.exe -B scripts/test_response_patch_idempotency.py
```

测试检查上游文件和本地文件的相关补丁重复执行是否稳定。它不验证 QQ 在线、模型效果或提醒送达。

确认配置后可以手动在 `.runtime/MaiBot` 中执行 `uv run bot.py`。仅当你已经同意连接并响应测试 QQ 消息时，才启用 NapCat Adapter。

`scripts/ensure_bot_running.ps1` 会应用持久化补丁，并启动／守护 MaiBot 和 NapCat；其中 NapCat 安装路径和端口需要按自己的电脑核对。`scripts/install_autostart.ps1` 会安装登录启动项并启动守护，不是普通的只读配置脚本，新同伴不要一上来就执行。

## 如何同步成果

见 [协作与交接](docs/协作与交接.md)。注意 `.runtime` 被忽略：**只提交外层仓库不会自动上传你改过的 MaiBot 或插件源码**。需要重新导出补丁或维护独立上游分支，并同步持久化脚本；不能覆盖队友整个运行目录。

项目任务见 [完整计划书](docs/Heart-Heart-Heart项目计划书.md)。修改前先说明入口、输入输出和预期效果；改完记录验证证据，不把模型一句“已完成”当作功能成功。

## 隐私与发布范围

这是公有发布副本，使用全新提交历史。没有携带原电脑 Git 历史、密钥、登录态、聊天／记忆数据库、表情包、日志、缓存、安装包或已移除的 Codex Provider。真实账号、群号及本机路径已替换；原电脑的文件与运行数据不受发布副本影响。

不要上传填好密钥的配置、真实群聊截图、备份或日志。历史 API 探测脚本可能产生计费请求，只能自行确认后手动运行。

## 来源与许可证

MaiBot、NapCat Adapter、提醒、戳一戳、语音插件是各自作者的成果。精确版本见 `upstreams.json`；公开仓库中的补丁不代表重写了这些项目。

MaiBot 的许可证为 GPL-3.0，随附于 `licenses/maibot-LICENSE`；其他已获取的上游许可证分别存于 `licenses/`，保留原作者权利。NapCat Adapter 的固定检出未发现根目录 LICENSE，本仓库不擅自为其整体源码重新授权。共享与再分发时应分别核对对应上游条款。

本次发布不向整个混合仓库套用单一的新许可证。团队独立新增部分的正式授权方式待团队确定；仓库公开不意味着所有内容可以不受限制地再分发。
