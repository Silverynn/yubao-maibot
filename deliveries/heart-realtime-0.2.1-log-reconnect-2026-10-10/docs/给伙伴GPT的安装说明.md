# 给伙伴 GPT / Codex 的安装说明

目标：保留伙伴的声线、形象、角色设定、模型选择、记忆、配置和源码自改，升级已经接入 MaiBot 的个人 Live2D 实时扩展。无需重新安装QQ、ASR模型或语音包，不操作StoryRoom房间。

## 1. 先定位实际运行实例

从伙伴的启动脚本确认实际 MaiBot 和 VT 根目录，不能把仓库外层当运行目录。读取目标的 AGENTS.md。本文示例的目录是占位符，请换成实际路径：

```powershell
$maiRoot = 'D:\项目\实际MaiBot'
$vtRoot = 'D:\项目\实际Open-LLM-VTuber'
```

本增量要求先装过 `heart-2026-10-09` 的实时插件、宿主接缝和VT前端接缝。已装的独立选模增量保持现状。仅安装过StoryRoom PC插件的实例不满足条件；先比较基础包、按其说明补齐，涉及主程序修改时遵守目标AGENTS及伙伴许可。不要把测试参考 `maibot/src` 直接复制进去。

已验收版本为 MaiBot 1.2.5、SDK 2.8.1、VT 1.2.1。异版本必须审阅合并和重新验证；不能跳过版本错误就说兼容。

## 2. 检查包和已有接缝

在解压后的本包根目录运行；优先使用目标MaiBot既有Python虚拟环境，不创建新的环境：

```powershell
& "$maiRoot\.venv\Scripts\python.exe" tools/verify_delivery.py
& "$maiRoot\.venv\Scripts\python.exe" tools/check_integration.py --maibot-root $maiRoot --vt-root $vtRoot
& "$maiRoot\.venv\Scripts\python.exe" tools/install_delivery.py --maibot-root $maiRoot --vt-root $vtRoot
```

这些命令不启动服务、不调用模型。状态含义：`same`已是新版；`update`匹配上次公开基线；`new`为清单明确新增且目标不存在；`conflict`为缺失基线或自改，整批停止写入。

若插件目录重命名，安装器按 `_manifest.json` 的 `heart.realtime` ID定位唯一实例并原地更新。接缝预检手动加 `--plugin-dir '实际插件目录'`。VT继续保留已有 `HEART_REALTIME_PLUGIN_DIR` 指向它；若未配，应明确配置正确目录，不能创建第二个相同ID的插件。

## 3. 有冲突时先合并，不能强制覆盖

以 `baseline/` 为共同基线，目标现文件为伙伴修改，`patches/` 为本次变更。保留双方修改；尤其保留伙伴声线调用、表情映射及其自定义前端。不要用 `--force`、删除插件后重装、替换整个VT目录或重置Git。此安装器没有强制覆盖选项。

自改已手动合并时，继续人工核对11个目标、跑离线测试并备份后部署；不要修改安装清单伪装成已知指纹。11个文件以外的源码是参考，不自动应用。

## 4. 停机备份后应用

确认没有进行中的对话，正常停止MaiBot和VT。按伙伴意愿备份运行目录及配置/数据；安装器也会备份11个目标的旧源码。审查通过后：

```powershell
& "$maiRoot\.venv\Scripts\python.exe" tools/install_delivery.py --maibot-root $maiRoot --vt-root $vtRoot --apply --stopped --reviewed
```

三部分必须一起更新；只更新 `engine.py` 或只更新前端不算安装完成。命令会再次执行只读接缝预检，备份到各自 `.runtime/heart-realtime-increment-20261010-*`，含ROLLBACK清单。重复执行应为 changed=0；写失败回退本次已写源码。

不替换 `conf.yaml`、TTS工厂、`voice_status.py`、ASR提供商、`model_dict.json`、角色昵称、模型配置、插件settings/facts、Git、记忆/聊天/房间数据。伙伴原声线仍由自己的VT语音配置决定；没有标准健康接口的外部声线仍须实测，不误报为缺少本机Moonbell。

运行环境仍沿用原来的 `HEART_MAIBOT_ROOT` / `HEART_REALTIME_PLUGIN_DIR` 与原WebUI连接配置。ASR模型/解释器/依赖路径保留伙伴现值；不同Python二进制不混用，不复制发送方的虚拟环境或绝对路径。

## 5. 离线验证并恢复

```powershell
& "$maiRoot\.venv\Scripts\python.exe" tests/run_offline.py
node tests/test_frontend.mjs
```

离线Python需要既有aiohttp依赖。Windows异步测试需要本机环回套接字权限；若执行沙箱拦截socketpair，按环境许可运行，不能当成插件坏了或跳过测试。

用伙伴原启动方式恢复两端，不替换启动器或改成发送方的QQ/Live2D模式。网页Ctrl+F5，先关闭提前生成验证普通语音，再开启实时插件，最后按需开启提前生成。语音输出开关仍控制伙伴自己的声线。桥密钥由插件本机重新生成，不抄发送方密钥、不放进网页URL或上传日志。

`/health`应 ready=true、streaming_ready=true，并有 input_end_supported / connection_fence_supported。这里的健康就绪不代表物理麦克风或声线已验收。

## 6. 功能验收与交付结论

按 [功能与验收](功能与验收.md) 测普通回复、查询、草稿、补充改口、停止、断线重连；核对当前角色表情和原声线。QQ私聊/多人群聊继续普通链路，本增量不会给QQ提前插话或语音回复；真实QQ测试需伙伴自己授权。

阅读日志在MaiBot `data/heart_observation/logs/Live2D/`，卡片在 `data/heart_observation/消息卡片/`。同一语音从识别到心情/回复应只有一个区块；后台无关联事件汇总，不猜归属。原始SQLite/JSONL保留详细功能凭证，不清空或重建用户记录。

报告时区分：文件安装、离线回归、健康就绪、真人语音、真实模型、QQ、伙伴双机联调。发送方已做合成音频真实ASR/工具/模型测试，不等于伙伴的麦克风、声线或公网联调已经通过；本增量也没有新增通用联网搜索、完整Planner提前执行或StoryRoom完整Lv4.5。
