# Live2D 重进会话与模型错误说明

## 现象与原因

旧版 `vtuber/maibot_client.py` 每次都使用同一个 `vtuber_local_user`，并用 `restore=True` 打开 MaiBot 聊天。Open-LLM-VTuber 网页刷新后聊天框是空的，但 MaiBot 仍会按这个身份读取之前的对话。Heart 的临时情绪也按同一会话保存在本地，因此旧表情状态会回来。这不是“AI 有神奇记忆”，而是两边会话边界不一致。

## 现在如何工作

打开 Live2D 网页时，VTuber 的 WebSocket 会产生一个新的连接编号。安装脚本给固定版本的 WebSocket 入口及单人对话流程加两个很小的钩子：先通知 Heart“这是新网页”，再把连接编号传给 MaiBot 适配器。适配器使用每次不同的用户标识，并以 `restore=False` 开始新的 MaiBot 会话。Heart 把上一网页仍活跃的临时情绪标记为结束，原因写为“Live2D 网页重新进入”，再等待本次网页的第一句对话；不会把 QQ 或其他网页用户的状态拿来显示。

这是**新网页连接＝新对话**策略：同一连接内连续聊天可以延续；关闭后重进或刷新，旧网页聊天与临时情绪不会进入新网页。短暂网络掉线后的 WebSocket 重连也会被视为新对话，这是当前固定版本无法区分重进与自动重连的边界。旧记录和记忆数据库没有被删除，只是不再自动用于新网页。当前新网页也不继承旧网页的个人长期事实；若将来需要跨网页保留已确认的长期事实，应单独增加长期身份映射，不能恢复不可见的聊天上下文。一次只建议打开一个鱼宝网页；两个网页并用时，面板会跟随后打开的网页。

日志里可看到 `临时情绪结束`，原因是重进网页。新网页第一次发言后，Live2D 状态和日志进入独立的新会话；QQ 仍在自己的文件夹中。

## 日志里的其他报错

2026-09-29 的 MaiBot 日志显示，插件本身加载成功（`heart.memory-audit` 和 `heart.mood` 均已加载）。另有 `deepseek-v4.1-flash` 的连接错误和服务拒绝访问、心情评估达到调用方时限，以及 NapCat 离线/断连。这些不会由“重进会话”修复；尤其模型连接错误会让 MaiBot 收不到文字回复。请先在 MaiBot 模型设置中检查提供商可用性、账户权限/余额、网络或代理，再用一次普通网页对话复测。不要把真实密钥和完整对话日志上传仓库。

## 给同伴的安装与验证

1. 停止 MaiBot 和 Open-LLM-VTuber，备份 Open-LLM-VTuber 的 `maibot_client.py`、`heart_bridge.py`、`src/open_llm_vtuber/agent/agents/maibot_agent.py`、`src/open_llm_vtuber/websocket_handler.py`、`src/open_llm_vtuber/conversations/single_conversation.py`。
2. 从新的 Heart 交付包将 `extensions/heart_shared/storage.py` 安装到 MaiBot 的 `heart_shared/storage.py`；其余 Heart 安装照主交付说明。将交付包中的 `vtuber/maibot_client.py`、`vtuber/heart_bridge.py`、`vtuber/src/open_llm_vtuber/agent/agents/maibot_agent.py` 覆盖到**对应的 VTuber 文件**。已有本地改动时先比较，勿整目录覆盖。
3. 在 VTuber 根目录运行包内的 `vtuber/scripts/install_heart_bridge.py`。它会检查固定版本锚点，给 WebSocket 和单人对话流程打最小钩子；版本不符会报错而不修改。然后正常启动 MaiBot 与 VTuber。
4. 网页里说两句、记下当前情绪；刷新网页。面板应先显示等待本次首次对话，不再显示旧话题。再发新消息，新回复不应提及旧网页中、当前页面不可见的聊天内容。查看 MaiBot 的 `data/heart_observation/logs/Live2D`，应有重进导致的临时情绪结束记录。

离线回归：Heart Python 测试、VTuber 代理测试和安装脚本幂等安装测试；没有向 QQ 发消息或调用真实模型进行本次验证。
