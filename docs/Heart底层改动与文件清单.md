# 底层改动清单：给合并代码的伙伴

本包不只是两个plugin.py。新增功能以插件及独立模块为主，原生接入点尽量小。请用安装器，勿漏拷共用模块。

## 一、修改的原生文件：三个文件，各新增两行

| 原生路径 | 改动 | 用处 | 对应补丁 |
|---|---|---|---|
| src/services/memory_service.py | 导入observed_memory_call；给MemoryService._invoke加装饰器 | 经过这个入口的记忆调用可被记录，指定写入可先核查冲突 | heart-memory-observer.patch |
| src/plugin_runtime/hook_catalog.py | 导入register_heart_hook_specs；加进注册列表 | 注册heart.memory.after_operation只读通知事件 | heart-memory-observer.patch |
| src/plugin_runtime/capabilities/registry.py | 导入resolve_capability；注册heart.memory.resolve | 将插件确认命令接到宿主，核对真实发言者并处理确认 | heart-memory-confirmation.patch |

“六行”仅指接入点，不是所有实现总共六行。实际逻辑在下述新增文件中。观察通知本身只读；写入前冲突关卡会主动拦截，并非整个扩展都是只读。

原生入口的关键形式：

```python
from src.services.heart_host_observer import observed_memory_call

@observed_memory_call
async def _invoke(...):
    ...  # 原有实现保持在原文件中
```

装饰器是在现有函数外包一层检查/记录。省略号仅用于讲解，不要拿示意代码覆盖原生实现。

## 二、新增文件的部署映射

| 包内路径 | 目标MaiBot路径 | 负责什么 |
|---|---|---|
| plugins/heart_memory_audit/ | plugins/heart_memory_audit/ | SDK事件处理、对话/记忆结果记录、确认命令、配置模型 |
| plugins/heart_mood/ | plugins/heart_mood/ | 心情插件、评分引擎、后台AI评估、界面配置模型 |
| extensions/heart_shared/ | heart_shared/ | SQLite事件/状态、中文日志渲染、冲突状态机 |
| extensions/heart_host_observer.py | src/services/heart_host_observer.py | 包装原生调用，写入前检查，结束后通知 |
| extensions/heart_memory_backend.py | src/services/heart_memory_backend.py | 宿主身份核实、模型判断、私聊、原生记忆操作适配 |

### plugin.py / engine.py / appraiser.py的区别

- 心情plugin.py：告诉MaiBot“何时调用我”，定义配置界面的字段，追加当次回复风格。
- engine.py：同一个持久化心情值，去重、恢复、冷却、总上限和保存。
- appraiser.py：AI请求、完整评分解析、置信度、倍率/方向上限、排队和诊断。不是把模型当作可以任意改数据库的管理员。
- readable.py：将结构化事件转成简单中文；内部细节保留数据库而不是铺满txt。

## 三、与原生记忆的合作方式

### 普通记忆调用

```text
原生提取/查询 → MemoryService._invoke → Heart观察桥 → 原生A_memorix
                                            ↓ 返回后
                                     记忆审计插件 → 中文日志
```

检索返回哪些候选、服务报告哪些存储结果，来自真实调用结果。还通过已有planner/replyer钩子识别模型输入中的记忆参考，不能据此声称模型最终“采用了”全部记忆。

### 人物事实冲突

写入前由before_memory_write筛选适用操作；归属明确的person_fact先检索本人旧事实并判断冲突。没有明确冲突时放行原生写入；不确定/失败时暂缓。明确冲突时生成受限原生计划、私聊原用户、持久化待确认请求。

用户私聊命令经heart.memory.resolve进入宿主，核对真实缓存消息的用户和私聊会话、编号、状态和有效期；确认后再次核对旧事实和计划未变化。先以原生计划的幂等外部编号确认新事实存储，再调用memory_correction_admin执行旧事实过时标记和画像刷新。

这是复用原生纠正接口，不是Heart自己去删A_memorix数据库行。取消、超时、执行中断和部分失败有不同状态，不应全部当作“更新成功”。

## 四、没有改什么

- 没有修改src/A_memorix实现、原生数据库结构、向量算法或原生事实提取Prompt。
- 没有覆盖机器人永久人设；心情风格只是追加当次extra_prompt。
- AI心情使用现有llm.generate能力，没有为这次400修复改LLM底层客户端。
- 没有把现在读取到的原生_log_length_truncation告警函数当作我们新增代码。
- 不包含其他人的src/chat/replyer/maisaka_generator_base.py、src/chat/utils/utils.py和相关测试改动。
- 不把原生“删除记忆”扩展成删除所有日志；日志仍包含历史原文。

## 五、数据与权限

新增data/heart_observation/events.sqlite3保存对话快照、事件、心情、去重状态与heart_proposals待确认流程。它不是原生长期记忆库，也不是把原生记忆全量迁走。

记忆插件清单声明heart.memory.resolve与send.text；心情声明llm.generate。安装包里的config.toml是默认配置，不是原使用者的账号/密钥配置。

## 六、合并、升级与撤销

安装器先对两份patch做git apply --check或反向检查，再检查自己管理的文件指纹；发现未知修改停止。运行配置始终保留。因此旧配置里的6秒等待不会被默认20秒自动覆盖。

合并时请同时带上两个plugins目录、extensions目录、两份patch、安装器和测试；只合并外层合作仓库中的源文件后，还需向真正运行目录部署。不要把整个运行目录或.git历史当作这个扩展发布。

上游升级后先在副本测试，不绕过失败检查。SDK和调用接口可能变，原生参考块标记也可能变，日志空参考不证明没有使用任何记忆。

回退先停机并关闭两插件。若使用反向patch，先在目标目录对两份patch执行反向检查，通过后才应用；只撤销这里列出的六行。不要git reset --hard或覆盖整个src。新增文件可以先保留不用，确认无依赖再处理。数据应单独备份，不随代码回退删除。

朋友如果需要完整团队机器人，还需按团队基础仓库准备其余依赖与定制；本扩展不是整机克隆。GitHub 合并只更新外层源码，运行实例仍需执行安装器部署。
