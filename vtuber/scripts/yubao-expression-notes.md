# 鱼宝的对话表情与标点

普通回复使用 model_dict.json 中的 neutral（当前为“平静”）。代理只依据明显语气词推断视觉反应，一轮最多一次明显表情；它不代表 MaiBot 内部心情状态。

frontend/yubao-expression-timing.mjs 在前端实际设置表情时开始计时。“开心兴奋、生气、悲伤、感叹号”最多保持 3000 毫秒后回到“平静”。新的表情会取消旧计时器，手动回到默认表情也会取消计时器。切换角色不会让旧计时器影响新角色。此规则只应用于 ds-whale-girl 模型。

可修改 EXPRESSION_HOLD_MS 调整持续时间。修改后浏览器按 Ctrl+F5 强制刷新。

本地前端只有编译产物，因此 scripts/apply_yubao_expression_timing.py 将入口 bundle 的 adapter.setExpression 接到独立模块。更新前端后需重新运行该脚本；它检查唯一锚点并支持重复运行。如果上游实现变化，它会报错停止，应先检查代码，不能强行替换。

src/open_llm_vtuber/agent/agents/maibot_agent.py 对没有句末标点的回复补标点，显示文字和朗读文字一致。保留已经存在的句末标点。补充了“我自己掏钱还不行”等主动提议后的反问，以及“行不行、好不好”等疑问结尾。这是保守的文字规则，不能保证识别所有中文语境。

离线验证（不会调用模型或发送聊天消息）：

```powershell
.\.venv\Scripts\python.exe -B scripts\test_agent_changes.py
node scripts\test_expression_timing.mjs
```
