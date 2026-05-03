# 会话修改插件 (Conversation Rewriter)

<p align="center">
  <img src="https://visitor.serveryyswys.top/cnt/astrbot_plugin_conversation_rewriter"><br>
  <strong>一个用于手动修正AstrBot对话内容的插件。支持修改最后一条用户消息或AI回复，让对话按照你期望的方向继续。</strong><br><br>
  <a href="https://opensource.org/licenses/MIT" target="_blank" rel="noopener noreferrer"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="MIT license"></a>
</p>

## ✨ 功能特点

- ✏️ **修改用户消息**：替换最后一条自己发送的消息，并让 AI 基于新内容重新生成回复。
- 🧠 **修改 AI 记忆**：直接编辑 AI 最后一条回复的内容，修正错误记忆，影响后续对话走向。
- 📝 **子串替换**：支持只替换消息中的部分文本，无需重新输入整条消息。
- 🔄 **自动重新生成**：修改用户消息后自动调用 LLM 生成新回复，保证对话结构完整。
- 🛡️ **异常容错**：LLM 调用失败时自动填充占位消息，维持对话历史结构。
- 🚫 **仅限私聊**：为避免群聊中多个插件互相干扰，限定仅在私聊中使用。

## 📋 支持的平台

- aiocqhttp

## 🚀 使用方法

### 基本指令

```
/rewrite user <旧文本> <新文本>
/rewrite ai   <旧文本> <新文本>
```

### 指令示例

假设当前对话为：
- User：`你好`
- Assistant：`你好，很高兴见到你，有什么可以帮你的吗？`

**1. 修改用户消息**
```
/rewrite user '你好' '你是谁'
```
效果：用户消息被替换为“你是谁”，AI 自动重新回答。

**2. 修改 AI 记忆**
```
/rewrite ai '有什么可以帮你的吗？' '有什么需要我帮忙的吗？'
```
效果：AI 回复中的对应文本被替换，下次对话将基于新记忆。

**3. 子串替换**
```
/rewrite user '好' '是谁'
```
效果：将“你好”中的“好”替换为“是谁”，用户消息变为“你是谁”。

### 参数包裹

当替换文本包含空格时，请使用以下包裹符号之一（两个参数必须使用相同符号）：
- 英文双引号：`"文本"`
- 英文单引号：`'文本'`
- 半角圆括号：`(文本)`
- 中文双引号：`“文本”`
- 中文单引号：`‘文本’`

### 注意事项

- 插件仅支持**私聊**使用，群聊中会提示“本插件仅支持私聊使用”。
- 若匹配到多处相同的子串，会提示“请提供更多特征文本以避免歧义”。
- 若未找到匹配的原文，会提示“未找到匹配的原文，请检查后重试”。

### 帮助指令

使用 `/rewrite_help` 查看插件帮助信息。

## 📦 安装方式

1. 在 AstrBot 插件市场中搜索 `astrbot_plugin_conversation_rewriter` 并安装。
2. 或手动克隆本仓库到 `AstrBot/data/plugins/` 目录下。
3. 重启 AstrBot 或使用热重载功能加载插件。

## ⚙️ 配置项

在 AstrBot WebUI 的插件管理页面中可修改以下配置：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `allow_modify_assistant` | bool | true | 是否允许修改 AI 记忆 |

## 📜 许可证

本项目基于 MIT 许可证开源。
