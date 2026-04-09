# Hermes WeChat Adapter

<p align="center">
  <img src="images/qrcode.png" width="280" alt="关注公众号获取更多 AI 实战内容"/>
</p>

<p align="center">
  <strong>扫码关注公众号「铁柱AGI」</strong><br/>
  获取更多 Hermes / OpenClaw / AI Agent 实战教程与前沿动态
</p>

---

<p align="center">
  <a href="#-快速开始"><b>快速开始</b></a> •
  <a href="#-功能特性"><b>功能特性</b></a> •
  <a href="#-安装步骤"><b>安装步骤</b></a> •
  <a href="#-已知限制"><b>已知限制</b></a> •
  <a href="#-常见问题"><b>常见问题</b></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-2.1.5-blue" alt="Version"/>
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License"/>
  <img src="https://img.shields.io/badge/python-3.10+-yellow" alt="Python"/>
  <img src="https://img.shields.io/badge/hermes-compatible-success" alt="Hermes Compatible"/>
</p>

---

## 关于本项目

**Hermes WeChat Adapter** 是一个为 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 开发的微信个人号接入适配器，基于 **iLink Bot API**（微信官方开放协议）实现。

> **这个项目由 Hermes Agent 自己编写、整理并提交到仓库。** 从代码开发到文档撰写再到 Git 提交，全部由 AI 自主完成。

### 为什么需要这个项目？

Hermes 原生支持 Telegram、Discord、Slack 等多个消息平台，但**不支持微信**。而微信是中国最主流的即时通讯工具，对于中文用户来说，无法接入微信意味着失去了最重要的 AI 交互入口。

本项目填补了这个空白。

---

## 工作原理

### 架构总览

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐     ┌──────────┐
│   微信 App   │ ←→  │  iLink Bot API   │ ←→  │   weixin.py     │ ←→  │  Hermes  │
│  (你的手机)  │     │ (微信官方协议)    │     │  (本适配器)      │     │ Gateway  │
└─────────────┘     └──────────────────┘     └─────────────────┘     └──────────┘
                            ↓                        ↓                      ↓
                     https://ilinkai          长轮询 getUpdates         AI Agent
                     ..weixin.qq.com          sendMessage              自动回复
```

### 核心机制

| 组件 | 说明 |
|------|------|
| **iLink Bot API** | 微信官方开放协议，提供消息收发能力 |
| **weixin.py** | 1488 行 Python 适配器，完整的消息生命周期管理 |
| **长轮询 (Long Polling)** | 35 秒超时的 getUpdates 循环，实时接收新消息 |
| **AES-128-ECB 加密** | 图片/文件上传下载均通过 CDN 加密传输，自动解密 |
| **context_token** | 每条消息携带的上下文令牌，回复时必须回显 |
| **QR 扫码登录** | 终端 ASCII 二维码 + URL 备选，过期自动刷新（最多3次） |

### 消息流转过程

1. **接收消息**: weixin.py 以长轮询方式调用 `getUpdates` 接口
2. **解析内容**: 提取文本、图片、语音、文件等，处理引用消息
3. **媒体下载**: AES 解密 CDN 数据 → 缓存到本地 → 交给 Agent
4. **派发给 Hermes**: 封装成 `MessageEvent` 交给 Gateway 调度
5. **AI 处理**: Agent 思考并生成回复
6. **发送回复**: 通过 `sendMessage` 接口将结果发回微信

---

## 功能特性

### ✅ 已完整实现

| 功能 | 说明 |
|------|------|
| 文本消息收发 | 超长消息自动分段（4000字/段），段落边界智能切分 |
| 图片收发 | AES-128-ECB 加密 CDN 上传/下载，自动解密，支持多图 |
| 视频接收 | 下载并缓存为 .mp4，Agent 可用 vision 分析 |
| 语音消息接收 | 使用微信内置语音转文字（voice_item.text） |
| 文件收发 | 支持任意格式（PDF/DOC/XLS/ZIP 等），AES 解密 + 原始文件名保留 |
| 引用消息（文本） | 自动提取引用文本并拼接前文 |
| 引用消息（媒体/文件） | 描述引用的文件名+大小/图片尺寸，同时下载引用的原始文件给 Agent |
| 正在输入指示 | 基于 typing ticket（getConfig 获取，10分钟缓存） |
| 消息去重 | message_id 5 分钟滑动窗口去重 |
| 权限控制 | open / allowlist / pairing 三种 DM 策略 |
| 二维码登录 | 终端 ASCII 二维码显示 + URL 备选，过期自动刷新（最多3次） |
| 会话恢复 | context_token 持久化到磁盘，重启后自动恢复 |
| 异常恢复 | 连续失败 3 次后 backoff 30s；session expired 自动暂停 10min 重试 |

### ⚠️ 基础实现（能用但有局限）

| 功能 | 当前状态 | 局限说明 |
|------|----------|----------|
| 群聊 | 已提取 group_id，区分 dm/group | **群聊路由和 @机器人逻辑未完善**：目前群聊消息能收到，但无法区分 @机器人和普通群消息，也无法只响应 @自己的消息。所有群聊消息都会触发回复。 |

### ❌ 不支持（协议或平台限制）

> 以下功能**当前版本无法实现**，原因标注在每项后面。不排除未来 iLink 协议升级或 Hermes 架构变化后可能支持。

| 功能 | 为什么不支持 |
|------|-------------|
| **Markdown 渲染** | 微信客户端不渲染 Markdown 语法。适配器已做 strip 处理（加粗 `**text**` → 纯文本 `text`，标题 `#` → 去掉，链接 → 显示原始 URL）。发送方需自行使用纯文本格式化 |
| **语音消息发送** | iLink Bot API 未开放主动发送语音的接口。只能接收语音（转文字），无法 TTS 合成后发回 |
| **表情包 / GIF / 贴图** | 微信特殊消息类型（emoji/sticker/gif），iLink 协议未开放这些类型的发送接口。收到此类消息会被静默忽略 |
| **朋友圈操作** | iLink Bot API 是单向消息通道，不支持朋友圈的发布、点赞、评论等操作 |
| **小程序 / 公众号消息** | iLink 协议仅覆盖个人号 C2C 消息，不涉及小程序和公众号的消息通道 |
| **多账号同时登录** | 当前设计为单账号模式（一个 account_id + token）。理论上可扩展多实例，但未测试且 config.yaml 需手动配置多个 account |
| **端到端加密 (E2EE)** | 微信部分聊天启用了 E2EE，iLink 返回的加密数据可能无法通过标准 AES-128-ECB 解密。这种情况极少见（主要是保密聊天），遇到时消息内容会丢失 |
| **消息撤回 / 编辑** | iLink 协议可以收到撤回通知（message_state 变更），但适配器当前未处理撤回事件。已发出的消息无法通过 API 撤回或编辑 |
| **在线状态显示** | 无法控制或查询微信账号的在线/离线状态。网关进程存活即代表「在线」 |
| **消息已读回执** | 微信的已读状态是双向同步的，但 iLink API 不提供已读回执接口 |

---

## 快速开始

### 前置要求

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) 已安装运行
- Python 3.10+
- [uv](https://docs.astral.sh/uv/) 包管理器
- 一个微信个人号（用于扫码登录）

### 给 Hermes Agent 的指令

把下面这段话发给你的 Hermes Agent，它会自动完成全部安装流程：

```
请帮我安装微信渠道适配器。

从 GitHub 克隆这个项目：
git clone git@github.com:RongleCat/hermes-wechat.git /tmp/hermes-wechat

然后读取 SKILL.md，按照里面的完整流程（8 个 Phase）逐步执行，
包括环境检查、依赖安装、适配器部署、核心文件 Patch、扫码登录、
配置写入、重启网关、连接验证。
```

---

## 安装步骤

### Step 1: 克隆项目

```bash
git clone git@github.com:RongleCat/hermes-wechat.git /tmp/hermes-wechat
cd /tmp/hermes-wechat
```

### Step 2: 安装 Python 依赖

```bash
cd ~/.hermes/hermes-agent
uv add aiohttp cryptography qrcode pillow
```

### Step 3: 部署适配器

```bash
cp /tmp/hermes-wechat/references/weixin.py \
   ~/.hermes/hermes-agent/gateway/platforms/weixin.py
```

### Step 4: Patch Hermes 核心

#### 4a. 编辑 `~/.hermes/hermes-agent/gateway/config.py`

在 `Platform` 枚举的 `WECOM = "wecom"` 之后添加：

```python
WEIXIN = "weixin",
```

#### 4b. 编辑 `~/.hermes/hermes-agent/gateway/run.py`

在 `_create_adapter()` 方法的 WECOM 分支之后添加：

```python
elif platform == Platform.WEIXIN:
    from gateway.platforms.weixin import WeixinAdapter, check_weixin_requirements
    if not check_weixin_requirements():
        logger.warning("WeChat: deps missing")
        return None
    return WeixinAdapter(config)
```

### Step 5: 扫码登录

```bash
cd ~/.hermes/hermes-agent && .venv/bin/python -c "
import asyncio, json, sys, os
sys.path.insert(0, '.')
from gateway.platforms.weixin import qr_login
result = asyncio.run(qr_login(os.path.expanduser('~/.hermes')))
if result:
    with open('/tmp/weixin_login_result.json','w') as f: json.dump(result,f,indent=2)
    print(json.dumps(result,indent=2))
else: sys.exit(1)
"
```

终端会显示 ASCII 二维码，用手机微信扫描并在微信内确认登录。

### Step 6: 写入配置

登录成功后，编辑 `~/.hermes/config.yaml`，添加：

```yaml
platforms:
  weixin:
    enabled: true
    extra:
      account_id: "从登录结果获取"
      token: "从登录结果获取"
      base_url: "https://ilinkai.weixin.qq.com"
      dm_policy: "open"
      allow_from: []
    home_channel:
      platform: "weixin"
      chat_id: "从登录结果获取"

platform_toolsets:
  weixin:
  - hermes-cli
```

在 `~/.hermes/.env` 添加：

```
GATEWAY_ALLOW_ALL_USERS=true
```

### Step 7: 重启网关

```bash
hermes gateway restart
```

### Step 8: 验证

```bash
grep -i "weixin" ~/.hermes/logs/gateway.log | tail -15
```

看到以下日志即表示成功：

```
weixin: adapter connected (account=xxx, base=https://ilinkai.weixin.qq.com)
weixin: starting poll loop (account=xxx)
weixin: inbound from=xxx text_len=xx images=0
```

---

## 常见问题

<details>
<summary><b>启动报 TypeError: get_hermes_dir() missing arguments</b></summary>

适配器已使用 `get_hermes_home()` 替代旧版 API，无需额外处理。
</details>

<details>
<summary><b>微信发消息后报 KeyError 'weixin'</b></summary>

`config.yaml` 的 `platform_toolsets` 缺少 weixin 条目。参考 Step 6 添加。
</details>

<details>
<summary><b>提示 "No home channel is set for WeChat"</b></summary>

weixin 平台配置缺少 `home_channel`。参考 Step 6 添加 `home_channel` 配置段。
</details>

<details>
<summary><b>提示 "All unauthorized users will be denied"</b></summary>

需要在 `.env` 中设置 `GATEWAY_ALLOW_ALL_USERS=true`，或在 config.yaml 中配置 `allow_from` 白名单。
</details>

<details>
<summary><b>日志出现 session expired (errcode=-14)</b></summary>

Token 过期或服务端会话失效。适配器会自动暂停 10 分钟后重试；如持续失败需重新扫码登录。
</details>

<details>
<summary><b>二维码过期没来得及扫</b></summary>

适配器会自动刷新二维码（最多 3 次），过期后终端会打印新的二维码 URL。
</details>

<details>
<summary><b>Agent 回复"图片没传成功"或"图片加载失败"</b></summary>

这是微信 CDN 加密导致的。**v2.1.4+ 已修复** —— 适配器会自动检测 aes_key 并执行 AES-128-ECB 解密后再缓存。如果仍然出现此错误，检查：
1. `file ~/.hermes/image_cache/img_xxx.jpg` 是否显示 `JPEG image` 而非 `data`
2. `xxd img_xxx.jpg | head -1` 是否以 `ffd8ff` 开头（JPEG）
如果仍显示乱码，说明该图片使用了非常规加密方案（如 E2EE），当前无法处理。
</details>

<details>
<summary><b>引用了文件/图片但 Agent 说看不到</b></summary>

**v2.1.5+ 已修复** —— 旧版在处理引用消息时会直接丢弃被引用的媒体信息。新版会：
1. 在文本中注入描述：`[引用文件: xxx.pdf (130427字节)]`
2. 同时下载被引用的原始文件并加入 media_urls 列表
如果仍有问题，确认 weixin.py 版本 >= v2.1.5（文件末尾有版本注释）。
</details>

<details>
<summary><b>网关无限重启循环，日志只有 Starting...</b></summary>

通常是 weixin.py 有语法错误导致 import 崩溃。最常见原因：**f-string 中使用了反斜杠**（Python 3 不允许）。检查方式：
```bash
cd ~/.hermes/hermes-agent && .venv/bin/python -c "from gateway.platforms.weixin import WeixinAdapter; print('OK')"
```
如果报 SyntaxError，修复语法后重启即可。
</details>

---

## 项目结构

```
hermes-wechat/
├── README.md                  # 本文件 - 项目介绍与安装指南
├── SKILL.md                   # Hermes Skill 定义 - Agent 执行指南
├── LICENSE                    # MIT 开源协议
├── .gitignore                 # Git 忽略规则
├── references/
│   └── weixin.py             # 微信适配器源码 (1488 行, v2.1.5)
└── images/
    └── qrcode.png            # 公众号二维码
```

---

## 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v2.1.0 | 2026-04-09 | 初始版本，基础消息收发 + QR 登录 |
| v2.1.1 | 2026-04-09 | 修复多图只收第一张、视频接收缺失 |
| v21.2 | 2026-04-09 | 新增文件收发、语音转文字 |
| v2.1.3 | 2026-04-09 | 新增 AES-128-ECB 加解密（修复致命的图片加密 bug） |
| v2.1.4 | 2026-04-09 | 修复临时文件泄漏、RAW_MSG_DUMP 日志污染、create_task 异常处理、QR 终端显示、群聊基础支持 |
| v2.1.5 | 2026-04-09 | 修复引用消息媒体/文件丢失（新增 _describe_ref_item + _extract_ref_items）、修复 f-string 反斜杠语法错误导致网关崩溃循环 |

---

## License

[MIT](./LICENSE) © 2026 [RongleCat](https://github.com/RongleCat)

---

<p align="center">
  <sub>Made with ❤️ by <a href="https://github.com/RongleCat">RongleCat</a> & Hermes Agent</sub><br/>
  <sub>This repository was written, organized and committed by Hermes Agent itself.</sub>
</p>
