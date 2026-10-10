# 第三方代码与资源

1. `pixi.js` 6.5.10（MIT），官方npm包。`pixi-live2d-display` 0.4.0（MIT），官方npm包。下载地址及SHA256在 `vendor-manifest.json`；许可证由 `scripts/setup_vendor.py` 同时保存到本机 `web/vendor`。不修改库源码。
2. MaiBot SDK：插件依赖现有SDK，通过公开生命周期、配置和 `llm.generate` 能力接口工作，没有复制MaiBot原生记忆代码。
3. Live2D Cubism Core：读取用户已安装Open-LLM-VTuber中的Core，仅用于本地预览；遵守其原始许可，源码交付不重新打包Core。
4. 月铃、旧鱼宝及其他模型：读取用户已有资源，不取得额外传播权。共享房间对外展示前，需要确认对应模型、纹理、音乐、背景等资源的许可。本版本场景背景是自己编写的CSS，没有下载背景图片。

本轮没有从其他跑团项目复制代码；有限世界线、任务裁决和共享房间为本项目实现。
