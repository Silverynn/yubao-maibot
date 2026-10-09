# Heart 独立选模增量更新

本包叠加在 `heart-2026-10-09` 交付之上（提交 `50b9eab`），不替代原包，也不改变实时插件策略或同伴已有语音包。

模型配置 → 模型任务新增 **心情评估模型 (heart_mood)** 和 **记忆查重／冲突判断模型 (heart_memory_dedup)**。页面心情列表已选模型时优先；留空使用原插件手填设置，手填也为空则utils。记忆新列表留空继用utils；覆盖人物事实同义／冲突与摘要语义查重。

新任务默认空，不替你选账号；不改回复、规划、memory摘要生成、事实提取、自然语言忘记或嵌入模型。保留原输出预算、群私聊评估频率、来源隔离、本人确认和失败待审核。心情日志增加实际模型、任务、选择来源。

包内有10个部署文件及各自更新前源码、逐文件补丁、受保护安装器、虚构离线测试。**不是只拷插件就能生效**；宿主配置、调度和能力入口也需要接入。不同宿主版本或已有修改会提示conflict，这是停止并合并的信号，不应强行覆盖。

先读 [给同伴AI助手的安装说明](给同伴AI助手的安装说明.md)。验证：

```text
python -B tools/verify_delivery.py
python -B tests/check_model_selection.py
python -B tests/check_installation.py
python -B tools/install_delivery.py --maibot-root "你的MaiBot目录"
```

最后一行仅预检。使用目标MaiBot自己的Python（离线检查需要其已有pydantic依赖），不要求重复安装依赖。

本包不含实际配置、API Key、模型权重、声音、聊天或记忆库。默认不启动服务、不调用收费模型。补齐后手动启动并刷新管理页面，在新任务勾选已有文本模型并确认保存。
