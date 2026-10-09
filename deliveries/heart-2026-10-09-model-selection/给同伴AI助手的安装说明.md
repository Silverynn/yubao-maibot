# 给同伴AI助手：安装独立选模增量

目标：让用户在MaiBot模型配置中独立选心情、人物事实／摘要查重模型。先阅读原 `heart-2026-10-09` 安装说明；本次不重装原交付或语音包。

## 比较与安装

1. 定位用户实际MaiBot源码根目录，确认已安装heart.mood与heart.memory-audit以及前包所需的共享／宿主服务；不重装ASR、TTS、形象或依赖。
2. 校验 `SHA256.json`，运行README的两项离线测试，再用install_delivery仅预检。所有10个文件是一个整体，不能只装页面或插件说明。
3. 此包7个目标与前包交付源码逐字比较通过，3个此前未提供的宿主文件（default_model_config.py、utils_model.py、plugin_runtime/capabilities/core.py）提供更新前参考，不假设与你本机一致。
4. 有conflict则整批停止，逐文件参考baseline与patch，只合并本次两个任务、空列表继用、heart.mood专用路由、两处查重task_name和诊断字段；保留本机其他修改。尤其保留你的SDK能力协议、模型网络参数、回复显示及实时接缝。不能把此包整份config.py当成跨版本兼容补丁。
5. 检查实际主程序AGENTS.md与用户授权。若本机已知基线全部匹配，在停服务、审查之后运行：

```text
python -B tools/install_delivery.py --maibot-root "你的MaiBot目录" --apply --stopped --reviewed
```

安装器核对SHA及更新前内容，未知改动不覆盖，写入失败回退，备份在目标 `.runtime/heart-model-selection-20261009-*`。不启动服务，也不修改实际TOML或数据库。重复安装零修改。

## 合并的关键点

- 模型配置版本本例1.17.11；若你的版本已更高，按你本机规则递增，不能倒退。沿用已有配置升级，不添加ConfigUpgradeHook，不替换实际配置文件。
- 两个任务默认model_list空并默认可见；无需修改前端硬编码列表，匹配版本的WebUI按Schema生成模型选择器。保留中文／英文／日文标签。
- 原生调度器两个空任务继用utils全部设置。心情宿主只对heart.mood无工具、原task_name为utils的评估改路由；有中央选择则优先于旧手填model_name。其他插件、其他显式任务及带工具接口保持。
- 人物事实检查和摘要语义检查都使用heart_memory_dedup，不能漏一处。只改task_name，不放宽规则和结果校验；不把摘要生成memory改成查重任务。
- 原生请求参数层级保持：心情预算/温度/等待由插件给出；摘要预算/温度/总等待由记忆插件给出；人物事实判断取任务预算且受插件等待约束。模型级max_tokens仍可能覆盖，不要为“省额”擅自降低输出上限。
- 心情diagnostics记录model_name、model_task、model_selection_source，不保存隐藏推理。记忆模型原生日志按heart.memory_conflict、heart.summary_semantic查看实际模型和用量。

## 手动验收

正常启动后刷新管理页，打开模型配置→模型任务，确认两个中文入口。先用已有可用文本模型保存，观察下一次评估日志；清空中央心情选择后应恢复旧手填，再清空旧手填则utils。群聊评估仍按用户本机条数／时间设置合并，不强制逐条。

人物事实：用原有记住功能保存测试用的本人偏好，再换一种说法复述，若召回旧事实并进入AI检查，heart.memory_conflict显示查重任务所选模型。完全一致可能直接规则判重，不应误判成入口失效。

摘要：正常互动形成后续摘要且有旧摘要候选时，heart.summary_semantic显示同一查重选项所选模型；切换模型不会主动重放历史。模型返回失败或不确定仍留候选待审核。

本包仅验证源码选模与安装保护；未在同伴电脑、真实QQ或收费模型验证。保持现有语音包，心情和记忆任务不是ASR/TTS设置。
