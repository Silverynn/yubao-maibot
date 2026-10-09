# 给同伴 AI 助手：保留现有语音包的安装说明

请先完整阅读本文件、README、目标项目 AGENTS.md 和 `INSTALL_MANIFEST.json`，然后检查实际运行目录及 Git 状态。用户机器的绝对路径不适用于你的机器。

## 最重要的要求

1. 同伴已经接入自己的语音包。先查实际 ASR、TTS、播放方式和入口；保留其声音、模型、密钥、地址、配置、虚拟环境和启动脚本。不要默认安装本包 `speech/`，不要把现有 TTS 换成 Moonbell，不要复制别人的 `conf.yaml`、`model_dict.json` 或任何运行数据。
2. 保留同伴自行修改的心情/表情适配。相对 10 月 1 日基线，本机心情插件源码没有变化，不需要为升级实时功能替换它。共享日志库会被心情使用，仍要核对接口和本地变更。
3. 此包依赖原 Heart 集成。只复制 `heart-realtime` 插件目录不够；必须配对 MaiBot 宿主接口、VT 输入/回复/播放接口和浏览器 VAD/播放接缝。
4. 不用删除现文件、降级依赖、清空记忆、降低输出预算来绕过冲突。修改宿主前遵守她项目的许可要求；上传包不等于同伴已授权安装。

## 第一步：识别三条语音链路

| 链路 | 现有方案优先 | 此包作用 |
| --- | --- | --- |
| 最终语音识别 | 原 ASR 完成整段转写，作为正式输入 | 保留，只将结果与当前发言编号绑定 |
| 流式临时识别 | 如果原 ASR 能持续返回临时文字，评估适配该协议 | 当前实现默认使用独立 Zipformer 临时识别；不是通用 ASR 插件注册器 |
| 语音输出 | 原 TTS 及原声音 | 只接取消、输出世代和播放回执，不改声音模型 |

如果“语音包”其实只包含 TTS，它并不提供临时识别。不要因为已经能说话，就跳过流式输入依赖检查。

当前默认流式实现通过 `stream_asr.py` 和 `runtime_config.py` 启动单独的本机服务。已有后端即使支持流式，也不能只填一个任意 URL 就声称已接上；要明确实现 PCM 输入、partial/final 事件、发言编号、关闭/取消和故障边界的协议适配并测试。若暂不做此适配，可在独立环境补充本包的流式识别，不覆盖原 ASR/TTS。

## 第二步：三方比较和完整性

先验证包：`python tools/verify_delivery.py`。

比较 `heart-2026-10-01`、本包、同伴当前源码。`INSTALL_MANIFEST.json` 每条记录含旧/新指纹与 `baseline_source`；前包未提供过的原生文件，基线是本项目固定上游版本，不是猜测同伴正在使用的版本。对照 `baseline/` 与对应 `patches/`。

默认只读预检：

```text
python tools/install_delivery.py --maibot-root "本机实际MaiBot目录"
```

默认查看共享库、记忆入口、实时插件、宿主、模型管理及显示变更；不选心情、QQ ASR、VT 或声带参考。`conflict` 会阻止整批安装，退出码为 2。它也可能表示缺少前提文件，不是“可放心当新文件覆盖”。

如果只是先接入实时能力，可显式选择 `--components shared,realtime,native`，但 `native` 文件之间仍有依赖，必须读清单。模型服务代理/模型页属于 `model`，显示方案 B 属于 `display`，可单独审阅选择，不需要强迫同伴采用本地聊天显示习惯。

预检通过、已备份、已获同伴要求的许可并停服务之后，安装器才接受：

```text
python tools/install_delivery.py --maibot-root "本机实际MaiBot目录" --apply --stopped --reviewed
```

安装器自动备份代码到目标 `.runtime/heart-delivery-20261009-*`，有回退清单，写入失败撤回本轮已写代码；不回滚或清理数据。人工合并后文件可能不等于包内指纹，这是合理的，要检查接口并重新测试，不要为了让预检变绿抹掉自改部分。

## 第三步：合并 MaiBot 接口

- `src/services/heart_realtime.py`：绑定同一 Live2D 访客会话、世代、取消和证据接入；绑定返回 `api_version=1` 与当前角色称呼。
- `heart_realtime_drafts.py`：准备/采用/丢弃草稿，晚到材料进入正式 Replyer；关闭开关和 QQ 会话仍走原模型。
- `capabilities/registry.py`：注册 `heart.realtime`；`send_service.py`：拦截旧世代回复与延迟提交播放结果。
- `maisaka_generator_base.py`：只移植实时生成调用及正常标点提示。这个包已经排除本机额外“傲娇/嘴硬”等风格追加，不要覆盖同伴原有人设逻辑。
- `heart_shared`：语音、实时、草稿、播放和模式记录共用审计。注意不能把 `plugin_loaded` 当成当前会话 `plugin_enabled`。
- `heart_live2d_delivery.py`：只跳过已识别的 Live2D 会话的模拟打字等待，QQ不改变。

本包没有重新带入已撤销的学习停用、输出预算缩减、启动历史限制或私聊节流。群聊心情 30 秒/16 条、群上下文 28/私聊 32 是作者本机配置选择，不会写入同伴配置；是否采用由她决定。`trust_env` 默认 true，不批量绕过她的代理。

## 第四步：VT 与现有声音的接入

VT 文件默认不安装。先看差异，再用 `--vt-root "本机VT目录" --components vt` 单独预检；未知自改文件需人工合并，特别是 `tts_manager.py`、`single_conversation.py`、`websocket_handler.py` 与 `maibot_agent.py`。

保留原 `tts_engine`，通过其现有接口合成。移植 `annotate_payload`、`eligible_payload`、发言编号与取消流程；通过原播放器上报实际 playing/completed/stopped。不要把“音频生成完成”记成“用户已经听完”。被取消的云端 TTS 可能仍计费，插件不保证撤回远端费用。

VT 进程配置 `HEART_MAIBOT_ROOT` 指向本机 MaiBot；自定义插件目录可设 `HEART_REALTIME_PLUGIN_DIR`。插件启动产生自己的密钥/发现文件，不能复制别人电脑的 `runtime-location.json` 或 bridge key。

字幕默认使用当前形象的 `live2d_model_name`；如需要角色昵称，可设置 `HEART_CHARACTER_NAME`。表情由她当前形象目录发现和映射，保留自定义别名，不能使用作者形象的固定表情编号。`vt-legacy-reference` 的旧形象计时 shim 不是新安装必需项。

健康检查已改为可选：不配置时显示“沿用现有语音包，状态未接入”，不会轮询作者的 12394/12395 端口。需要检查时，分别配置本机 `HEART_VOICE_HEALTH_URL`、`HEART_ASR_HEALTH_URL`；接口需返回布尔 `ready`，可选 `available`。不同格式请适配，不能伪造 ready=true。健康未知不阻止原录音/合成链路。

浏览器先合并模块，再预检：

```text
python tools/prepare_frontend.py --vt-root "本机VT目录"
```

此工具只对已匹配构建准备定点修改，保留原 HTML 与声音按钮；所有锚点匹配才可加 `--apply`，并自动备份。它覆盖 VAD 开始/临时 PCM、音频世代过滤、队列取消、实际播放与文字可见回执、模块加载。若是不同构建/已有自改播放器，停止并在源组件中移植这些接口，再构建；不要覆盖她整个 JS/HTML。旧 Heart 播放事件接缝若尚未存在，也需要先完成对应移植。

`check_integration.py` 是静态接口检查，不是网络健康或浏览器验收：

```text
python tools/check_integration.py --maibot-root "本机MaiBot目录" --vt-root "本机VT目录"
```

## 第五步：只在确实缺能力时补充识别

流式识别配置在本机插件 `settings.json`，从 `settings.example.json` 复制并修改本机路径。指定自己的 Python、模型目录、独立依赖目录；空 `asr_dependencies` 表示使用该 Python 环境自身依赖。运行插件 `check_compatibility.py` 检查解释器与模型，不跨系统复制二进制依赖。要求当前协议所需 sherpa 流式 API，已测 sherpa 1.13.8；保留现有最终 ASR 环境的版本。

只有确认没有可用的最终识别服务且同伴选择采用此方案，才考虑 `asr` 组件和 `speech/`。可选 SenseVoice 服务模型放在该目录的 `models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/`，使用选定环境执行 `python speech/server.py`，默认本机 12395。模型与依赖不在包里；先核对已有资源，不能重复安装。QQ 使用此插件时只替换原语音消息为文字并记录来源/消息编号，不添加 QQ 语音回复。

## 第六步：验收与回退

先在隔离测试运行回归，再由同伴实际录音/播放。按 `功能测试与日志.md` 验证关闭插件、仅实时、实时加提前生成三种模式；必须测试她自己的声音和至少两个形象。群聊多个人交替文字/语音，检查身份关联，“停一下”不能取消别人或 Live2D 的任务。

若自定义 ASR/TTS 回调无法满足取消/回执接口，明确报告缺哪项，不把本机模拟测试成绩当她电脑已通过。回退先停服务，按 `.runtime` 的旧代码备份与 `ROLLBACK.json` 恢复本轮文件；保留她的配置和升级期间产生的记忆/日志。
