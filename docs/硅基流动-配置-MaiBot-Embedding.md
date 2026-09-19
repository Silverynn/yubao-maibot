# 通过硅基流动为 MaiBot 配置 Embedding 模型

Embedding 模型是 MaiBot 的记忆功能所需组件：它用于把历史消息转换为可检索的内容，帮助机器人在后续对话中找回相关记忆。
因此我们需要获得一个embedding模型，这里使用siliconflow（免费）
（我在运行日志中看到了这个问题，询问AI后发现如果缺少Embedding模型会导致记忆功能出现一点问题？？？不知道你有没有这个问题）

## 1. 注册并登录硅基流动

1. 打开 [硅基流动控制台](https://cloud.siliconflow.cn/)。
2. 按页面提示使用手机号或邮箱注册、登录。
3.实名认证（不清楚此步是否必须，但是认证了能看到的模型更多）

## 2. 创建 API Key

1. 打开 [API Keys 页面](https://cloud.siliconflow.cn/account/ak)。
2. 点击 **新建 API 密钥**（或 **Create API Key**）。
3. 填写便于识别的名称，例如 `maibot`，然后创建。

## 3. 确认模型名称

打开 [硅基流动模型列表](https://cloud.siliconflow.cn/models)，确认 `BAAI/bge-m3`可用。本教程使用该模型；它是硅基流动 Embeddings 接口支持的模型之一。
（我已确认过了，此步直接跳过）

## 4. Maibot里导入模型

MaiBot Web UI 界面中->模型管理->模型设置->模型厂商里添加->提供商模板里选择siliconflow->填写你的API->添加模型里选择BAAI/bge-m3
之后在功能分配里的Embedding模型里选择BAAI/bge-m3，就算成功了

## 参考

- [硅基流动：快速开始](https://docs.siliconflow.cn/docs/userguide/quickstart)
- [硅基流动：Create Embeddings](https://siliconflow.readme.io/reference/createembedding)
- [MaiBot 配置示例：Embedding](https://github.com/Mai-with-u/MaiBot/blob/main/docs/installation_standard.md)
