# Hermes WeChat Adapter

<p align="center">
  <img src="images/qrcode.png" width="280" alt="关注公众号获取更多 AI 实战内容"/>
</p>

<p align="center">
  <strong>扫码关注公众号「铁柱的AI进化论」</strong><br/>
  获取更多 Hermes / AI Agent 实战教程与前沿动态
</p>

---

<p align="center">
  <a href="#-快速开始"><b>快速开始</b></a> •
  <a href="#-工作原理"><b>工作原理</b></a> •
  <a href="#-安装步骤"><b>安装步骤</b></a> •
  <a href="#-常见问题"><b>常见问题</b></a> •
  <a href="#%EF%B8%8F-贡献"><b>贡献</b></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-2.1.3-blue" alt="Version"/>
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License"/>
  <img src="https://img.shields.io/badge/python-3.10+-yellow" alt="Python"/>
  <img src="https://img.shields.io/badge/hermes-compatible-success" alt="Hermes Compatible"/>
</p>

---

## 关于本项目

**Hermes WeChat Adapter** 是一个为 [Hermes Agent](https://github.com/anthropics/hermes) 开发的微信个人号接入适配器，基于 **iLink Bot API**（微信官方开放协议）实现。

> **这个项目由 Hermes Agent 自己编写、整理并提交到仓库。** 从代码开发到文档撰写再到 Git 提交，全部由 AI 自主完成 —— 这本身就是 Hermes 能力的最好证明。

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
| **weixin.py** | 1275 行 Python 适配器，实现完整的消息生命周期管理 |
| **长轮询 (Long Polling)** | 35 秒超时的 getUpdates 循环，实时接收新消息 |
| **AES-128-ECB 加密** | 图片/文件上传时通过 CDN 加密传输 |
| **context_token** | 每条消息携带的上下文令牌，回复时必须回显 |
| **QR 扫码登录** | 通过二维码完成微信账号授权 |

### 消息流转过程

1. **接收消息**: weixin.py 以长轮询方式调用 `getUpdates` 接口
2. **解析内容**: 提取文本、图片、语音等，处理引用消息
3. **派发给 Hermes**: 封装成 `MessageEvent` 交给 Gateway 调度
4. **AI 处理**: Agent 思考并生成回复
5. **发送回复**: 通过 `sendMessage` 接口将结果发回微信

### 关于 Hermes 的插件系统

> **Hermes 目前不支持官方插件系统。**

Hermes 的平台适配器采用**内置注册制** —— 所有平台（Telegram、Discord、Slack 等）都硬编码在 `gateway/platforms/` 目录下，通过 `gateway/config.py` 的 `Platform` 枚举和 `gateway/run.py` 的 `_create_adapter()` 工厂方法统一管理。

这意味着要添加新平台，必须：

1. 在 `platforms/` 目录下放置适配器文件
2. 修改 `config.py` 添加枚举值
3. 修改 `run.py` 注册工厂函数
4. 安装额外的 Python 依赖

**这是一种「补丁式」扩展方式，不是官方支持的插件机制。** 未来如果 Hermes 引入插件系统，本项目的集成方式将会简化。目前这是唯一可行的方案。

---

## 快速开始

### 前置要求

- [Hermes Agent](https://github.com/anthropics/hermes) 已安装运行
- Python 3.10+
- [uv](https://docs.astral.sh/uv/) 包管理器
- 一个微信个人号（用于扫码登录）

### 给 Hermes Agent 的指令

把下面这段话发给你的 Hermes Agent，它会自动完成从拉取代码到配置的全部流程：

```
请帮我安装微信渠道适配器。按以下步骤执行：

第一步：从 GitHub 拉取项目代码
执行 git clone git@github.com:RongleCat/hermes-wechat.git 到临时目录，
然后查看 references/weixin.py 和 SKILL.md 的内容。

第二步：安装 Python 依赖
在 ~/.hermes/hermes-agent 目录下执行：
uv add aiohttp cryptography qrcode pillow

第三步：部署适配器文件
把 references/weixin.py 复制到 ~/.hermes/hermes-agent/gateway/platforms/weixin.py

第四步：修改 Hermes 核心文件
在 gateway/config.py 的 Platform 枚举中 WECOM = "wecom" 后面加一行：
WEIXIN = "weixin",
在 gateway/run.py 的 _create_adapter() 方法中 WECOM 分支后面加一个 elif 分支：
elif platform == Platform.WEIXIN:
    from gateway.platforms.weixin import WeixinAdapter, check_weixin_requirements
    if not check_weixin_requirements():
        logger.warning("WeChat: aiohttp/cryptography not installed")
        return None
    return WeixinAdapter(config)

第五步：引导我扫码登录
生成登录脚本并用后台模式运行，把二维码 URL 展示给我。
我扫完码确认后，读取登录结果。

第六步：写入配置
把登录获得的 account_id、token、user_id 写入 ~/.hermes/config.yaml 的 platforms.weixin 配置段，
同时在 platform_toolsets 中加入 weixin 条目，在 .env 文件中加入 GATEWAY_ALLOW_ALL_USERS=true。

第七步：询问我是否重启网关
配置写完后问我是否需要重启，如果我说重启就执行重启操作。

第八步：验证连接
重启后检查日志确认微信连接成功。
```

### 手动安装（如果不使用 Hermes 自动化）

如果你希望手动执行每一步，请参考下面的详细步骤。

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
        logger.warning("WeChat: aiohttp/cryptography not installed")
        return None
    return WeixinAdapter(config)
```

### Step 5: 扫码登录

```bash
cat > /tmp/weixin-login.py << 'PYEOF'
import asyncio, json, sys, os
sys.path.insert(0, os.path.expanduser("~/.hermes/hermes-agent"))
from gateway.platforms.weixin import qr_login

result = asyncio.run(qr_login(os.path.expanduser("~/.hermes")))
if result:
    out = "/tmp/weixin_login_result.json"
    with open(out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n=== 登录成功！信息已保存到 {out} ===")
    print(json.dumps(result, indent=2))
else:
    print("\n=== 登录失败 ===")
    sys.exit(1)
PYEOF

cd ~/.hermes/hermes-agent && .venv/bin/python /tmp/weixin-login.py
```

终端会输出二维码 URL，用手机微信扫描并在微信内确认登录。

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
pkill -f "hermes gateway" 2>/dev/null || true
cd ~/.hermes/hermes-agent && hermes gateway run
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

用另一个微信号给该账号发消息测试自动回复。

---

## 功能特性

| 功能 | 状态 | 说明 |
|------|------|------|
| 文本消息收发 | ✅ | 支持超长消息自动分段（4000字/段） |
| 图片收发 | ✅ | AES 加密 CDN 上传/下载 |
| 语音消息 | ✅ | 使用微信内置语音转文字 |
| 文件发送 | ✅ | 支持任意格式文件 |
| 引用消息 | ✅ | 自动提取引用上下文 |
| 正在输入指示 | ✅ | 基于 typing ticket |
| 消息去重 | ✅ | 5 分钟窗口去重 |
| 权限控制 | ✅ | open / allowlist / pairing 三种策略 |
| 二维码刷新 | ✅ | 过期自动刷新（最多 3 次） |
| 会话恢复 | ✅ | context_token 持久化 |
| 群聊 | 🚧 | 协议支持，待完善路由 |

---

## 常见问题

<details>
<summary><b>启动报 TypeError: get_hermes_dir() missing arguments</b></summary>

适配器已使用 `get_hermes_home()` 替代旧版 API，无需额外处理。如果仍然遇到此问题，请确认 Hermes 版本为最新。
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

完整踩坑手册见 [SKILL.md](./SKILL.md)。

---

## 项目结构

```
hermes-wechat/
├── README.md                  # 本文件 - 项目介绍与安装指南
├── SKILL.md                   # Hermes Skill 定义 - Agent 执行指南
├── LICENSE                    # MIT 开源协议
├── .gitignore                 # Git 忽略规则
├── references/
│   └── weixin.py             # 微信适配器源码 (1275 行)
└── images/
    └── qrcode.png            # 公众号二维码
```

---

## 验证 Checklist

安装完成后逐项确认：

- [ ] `uv list` 显示 aiohttp, cryptography, qrcode, pillow 已安装
- [ ] `gateway/platforms/weixin.py` 存在且可 import
- [ ] `gateway/config.py` 的 Platform 枚举包含 `WEIXIN`
- [ ] `gateway/run.py` 的 `_create_adapter()` 有 WEIXIN 分支
- [ ] `config.yaml` 有完整的 platforms.weixin 配置
- [ ] `.env` 有 `GATEWAY_ALLOW_ALL_USERS=true`
- [ ] 网关启动无报错
- [ ] 日志出现 `weixin: adapter connected`
- [ ] 日志出现 `weixin: starting poll loop`
- [ ] 微信发消息后有 `weixin: inbound from=` 日志
- [ ] Agent 能正常回复微信消息

---

## 贡献

欢迎 Issue 和 Pull Request！

- 发现 Bug？ → 提 [Issue](https://github.com/RongleCat/hermes-wechat/issues)
- 想改进代码？ → 提 [Pull Request](https://github.com/RongleCat/hermes-wechat/pulls)
- 有使用问题？ → 在 Discussions 中讨论

---

## Star 历史

<a href="https://star-history.com/#RongleCat/hermes-wechat&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=RongleCat/hermes-wechat&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=RongleCat/hermes-wechat&type=Date" />
   <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=RongleCat/hermes-wechat&type=Date" />
 </picture>
</a>

---

## License

[MIT](./LICENSE) © 2026 [RongleCat](https://github.com/RongleCat)

---

<p align="center">
  <sub>Made with ❤️ by <a href="https://github.com/RongleCat">RongleCat</a> & Hermes Agent</sub><br/>
  <sub>This repository was written, organized and committed by Hermes Agent itself.</sub>
</p>
