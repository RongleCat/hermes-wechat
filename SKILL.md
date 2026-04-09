---
name: hermes-weixin-setup
description: 在 Hermes Agent 上安装微信 (iLink Bot API) 平台适配器，完成从依赖安装到扫码登录的全流程。一键安装，扫码即用。
category: devops
---

# Hermes 微信 (iLink Bot API) 适配器 — 完整安装指南

> **适用场景**: 用户需要在 Hermes Agent 上接入微信个人账号，实现 AI 自动收发消息
> **协议**: iLink Bot API (微信官方开放协议)
> **适配器**: `weixin.py` (见附件 `references/weixin.py`)
> **预计耗时**: 5-10 分钟（含扫码等待）
> **分享说明**: 本 skill 可直接分享给其他 Hermes 用户，包含完整代码和流程

---

## 快速开始（给用户的引导话术）

当用户说「我要接微信」或「配置微信渠道」时，按以下流程引导：

### Phase 1: 环境检查

```
我来帮你接入微信渠道。先检查一下环境...
```

执行以下检查：

```bash
# 1. 检查 Hermes 是否存在
ls ~/.hermes/hermes-agent/gateway/platforms/ 2>/dev/null && echo "OK" || echo "MISSING"

# 2. 检查 Python 版本
python3 --version

# 3. 检查 uv 是否可用
which uv && uv --version
```

如果任何检查失败，告知用户需要先安装缺失的工具。

### Phase 2: 安装依赖

```bash
cd ~/.hermes/hermes-agent
uv add aiohttp cryptography qrcode pillow
```

### Phase 3: 部署适配器文件

**关键步骤**: 将 `references/weixin.py` 复制到 Hermes 平台目录：

```bash
cp <skill-path>/references/weixin.py ~/.hermes/hermes-agent/gateway/platforms/weixin.py
```

其中 `<skill-path>` 是本 skill 的安装路径，通常在 `~/.hermes/skills/devops/hermes-weixin-setup/`

### Phase 4: Patch Hermes 核心文件

#### 4a. 在 `gateway/config.py` 添加 WEIXIN 枚举

找到 `Platform` 枚举，在 `WECOM = "wecom"` 之后添加：

```python
WEIXIN = "weixin",
```

#### 4b. 在 `gateway/run.py` 注册适配器工厂

找到 `_create_adapter()` 方法中的 WECOM 分支，在其后添加：

```python
elif platform == Platform.WEIXIN:
    from gateway.platforms.weixin import WeixinAdapter, check_weixin_requirements
    if not check_weixin_requirements():
        logger.warning("WeChat: aiohttp/cryptography not installed")
        return None
    return WeixinAdapter(config)
```

### Phase 5: 扫码登录（交互式核心）⭐

这是最关键的步骤。告诉用户：

```
接下来需要你用微信扫码登录。我会启动登录进程，
生成二维码给你扫描。请准备好你的手机微信。
```

**方式 A：Agent 后台执行（推荐）**

生成并运行登录脚本：

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

**重要提示**：
- 必须使用 `background=true` 运行此命令（因为扫码需要时间）
- 用 `process(action="poll")` 监控进度
- 当用户看到二维码 URL 后，提醒他用手机微信扫一扫

**方式 B：备用方案（如果用户看不到二维码）**

如果后台运行导致用户无法看到二维码输出，告诉用户：

```
⚠️ 看起来终端输出被拦截了，无法显示二维码。
没关系，请按以下步骤操作：

1. 打开一个新的终端窗口（Terminal / CMD）
2. 复制粘贴以下命令并回车执行：
【粘贴单行登录脚本】
3. 终端会显示二维码 URL，用手机微信扫一扫并在微信内确认
4. 登录成功后，把终端输出的 JSON 内容复制发给我
```

单行登录脚本（让用户复制）：

```bash
cd ~/.hermes/hermes-agent && .venv/bin/python -c "import asyncio,json,sys,os; sys.path.insert(0,'.'); from gateway.platforms.weixin import qr_login; r=asyncio.run(qr_login(os.path.expanduser('~/.hermes'))); open('/tmp/weixin_login_result.json','w').write(json.dumps(r,indent=2)) if r else sys.exit(1); print(json.dumps(r,indent=2))"
```

用户把 JSON 发回来后，继续 Phase 6 写入配置。

**扫码后的状态流转**：
1. `wait` → 等待扫码（显示 `.`）
2. `scaned` → 已扫码，等确认（显示 `👀 已扫码，请在微信中确认...`）
3. `confirmed` → 登录成功！（显示 `✅ 微信连接成功！`）
4. `expired` → 二维码过期，自动刷新（最多 3 次）

### Phase 6: 写入配置

登录成功后，读取结果并写入 config.yaml：

```bash
cat /tmp/weixin_login_result.json
```

输出示例：
```json
{
  "account_id": "o9cq80yecxZhQHeAnocHatgkk_lo",
  "token": "xxxxxxxxxxxxxxx",
  "base_url": "https://ilinkai.weixin.qq.com",
  "user_id": "o9cq80yecxZhQHeAnocHatgkk_lo"
}
```

**写入 `~/.hermes/config.yaml`**：

在 `platforms:` 下添加：

```yaml
platforms:
  weixin:
    enabled: true
    extra:
      account_id: "<从登录结果获取>"
      token: "<从登录结果获取>"
      base_url: "https://ilinkai.weixin.qq.com"
      dm_policy: "open"          # open | allowlist | pairing
      allow_from: []
    home_channel:
      platform: "weixin"
      chat_id: "<user_id 从登录结果获取>"   # 如 o9cq80yecxZhQHeAnocHatgkk_lo@im.wechat
```

在 `platform_toolsets:` 下添加：

```yaml
platform_toolsets:
  # ... 其他平台 ...
  weixin:
  - hermes-cli
```

在 `~/.hermes/.env` 中添加：

```
GATEWAY_ALLOW_ALL_USERS=true
```

### Phase 7: 询问重启 ⭐

配置写入完成后，询问用户：

```
✅ 微信配置已完成！

配置摘要：
- account_id: xxxxxxxx
- dm_policy: open（允许所有人私聊）
- home_channel: 已设置

是否需要我现在重启网关以激活微信连接？
回复「重启」或「yes」我将立即重启。
```

如果用户确认重启：
```bash
# 停止现有网关（如果在运行）
pkill -f "hermes gateway" 2>/dev/null || true

# 启动网关
cd ~/.hermes/hermes-agent && hermes gateway run
```

### Phase 8: 验证连接

重启后，检查日志确认微信连接成功：

```bash
grep -i "weixin" ~/.hermes/logs/gateway.log | tail -15
```

期望看到的日志：
```
weixin: adapter connected (account=xxxxxxxx, base=https://ilinkai.weixin.qq.com)
weixin: starting poll loop (account=xxxxxxxx)
weixin: inbound from=xxxxxxxx text_len=xx images=0
```

**最终验证**：让用户用另一个微信号给该账号发一条消息，看 AI 是否自动回复。

---

## 完整安装脚本（一键版）

如果想一步到位，可以用这个完整脚本：

```bash
#!/bin/bash
set -e

HERMES_HOME="$HOME/.hermes"
HERMES_AGENT="$HERMES_HOME/hermes-agent"
PLATFORM_DIR="$HERMES_AGENT/gateway/platforms"

echo "=== Hermes 微信适配器一键安装 ==="

# Step 1: 检查环境
echo "[1/7] 检查环境..."
[ -d "$PLATFORM_DIR" ] || { echo "ERROR: Hermes 未安装在 $HERMES_AGENT"; exit 1; }
command -v uv >/dev/null 2>&1 || { echo "ERROR: uv 未安装"; exit 1; }
echo "✓ 环境检查通过"

# Step 2: 安装依赖
echo "[2/7] 安装 Python 依赖..."
cd "$HERMES_AGENT"
uv add aiohttp cryptography qrcode pillow 2>&1 | tail -3
echo "✓ 依赖安装完成"

# Step 3: 复制适配器（需要提前把 weixin.py 放好）
echo "[3/7] 部署适配器..."
SKILL_DIR="$HERMES_HOME/skills/devops/hermes-weixin-setup/references"
if [ -f "$SKILL_DIR/weixin.py" ]; then
    cp "$SKILL_DIR/weixin.py" "$PLATFORM_DIR/weixin.py"
    echo "✓ 适配器已部署"
else
    echo "⚠ 请手动复制 weixin.py 到 $PLATFORM_DIR/"
fi

# Step 4-6: 需要 Agent 执行 patch 和配置
echo ""
echo "[4-6/7] Patch 和配置需要 Agent 辅助完成..."
echo "请继续与 Agent 对话，它会帮你："
echo "  - Patch gateway/config.py (添加 WEIXIN 枚举)"
echo "  - Patch gateway/run.py (注册适配器工厂)"
echo "  - 引导你扫码登录"
echo "  - 写入 config.yaml 配置"
echo ""
echo "或者运行 /tmp/weixin-login.py 开始扫码登录"
echo "=== 一键安装脚本结束 ==="
```

---

## 已知问题 & 解决方案（踩坑手册）

### 坑 1: get_hermes_dir() 签名不匹配
**症状**: 启动时报 `TypeError: get_hermes_dir() missing 2 required positional arguments`
**原因**: 当前版 Hermes 的 `get_hermes_dir(new_subpath, old_name)` 需要 2 个参数
**修复**: 本适配器已使用 `get_hermes_home()` 替代（无参数版本），无需额外处理

### 坑 2: 登录命令被中断
**症状**: 前台跑 qr_login 时用户发新消息导致进程被 kill (exit code 130)
**修复**: 使用 `background=true` 启动登录进程，通过 `process poll` 等待结果

### 坑 3: MessageEvent 字段名错误 ⚠️ 最常见
**症状**: 微信发消息后报错：`TypeError: MessageEvent.__init__() got an unexpected keyword argument 'content'`
**原因**: 旧版用了 `content=text`，但正确字段是 `text`
**修复**: 本适配器已修复（第 1045-1050 行），使用正确的字段名：
```python
event = MessageEvent(
    source=source,
    text=text,                    # ✓ 正确
    message_type=MessageType.IMAGE if images else MessageType.TEXT,
    message_id=str(msg_id) if msg_id else None,
)
```

### 坑 4: KeyError 'weixin' — platform_toolsets 未配置
**症状**: 微信消息能收到但 agent 回复报错：`KeyError: 'weixin'`
**原因**: config.yaml 中 `platform_toolsets` 没有 weixin 条目
**修复**: 添加（见 Phase 6）

### 坑 5: No home channel for Weixin
**症状**: 回复提示 "No home channel is set for Weixin"
**原因**: weixin 平台配置中没有设置 home_channel
**修复**: 添加 home_channel（见 Phase 6）

### 坑 6: 用户被拒绝访问
**症状**: 日志显示 "No user allowlists configured. All unauthorized users will be denied."
**原因**: 默认需要用户白名单
**修复**: 设置 `GATEWAY_ALLOW_ALL_USERS=true`（见 Phase 6）

### 坑 7: 二维码过期
**症状**: 扫码太慢导致二维码过期
**修复**: 适配器会自动刷新二维码（最多 3 次），过期后会打印新的二维码 URL

### 坑 8: 会话过期 (errcode=-14)
**症状**: 日志出现 "session expired, pausing 10 min"
**原因**: token 过期或服务端会话失效
**修复**: 适配器会自动暂停 10 分钟后重试；如持续失败需重新扫码登录

---

## 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `gateway/platforms/weixin.py` | 新增 | 适配器主文件（1275 行） |
| `gateway/config.py` | 修改 | 加 `WEIXIN = "weixin"` 枚举值 |
| `gateway/run.py` | 修改 | 注册 WeixinAdapter 工厂函数 |
| `~/.hermes/config.yaml` | 修改 | 加 platforms.weixin + platform_toolsets + home_channel |
| `~/.hermes/.env` | 修改 | 加 GATEWAY_ALLOW_ALL_USERS=true |
| pyproject.toml / uv.lock | 修改 | uv add 自动更新 |

---

## 验证 Checklist

安装完成后，逐项确认：

- [ ] `uv list` 显示 aiohttp, cryptography, qrcode, pillow 已安装
- [ ] `gateway/platforms/weixin.py` 存在且可 import
- [ ] `gateway/config.py` 的 Platform 枚举包含 `WEIXIN`
- [ ] `gateway/run.py` 的 `_create_adapter()` 有 WEIXIN 分支
- [ ] `config.yaml` 有完整的 platforms.weixin 配置
- [ ] `.env` 有 `GATEWAY_ALLOW_ALL_USERS=true`
- [ ] 网关启动无报错
- [ ] 日志出现 `weixin: adapter connected`
- [ ] 日志出现 `weixin: starting poll loop`
- [ ] 微信发消息后有 `weixin: inbound from=` 日志（无 TypeError）
- [ ] agent 能正常回复微信消息（无 KeyError / No home channel 错误）

全部勾选 ✅ 即表示安装成功！

---

## 卸载指南

如需移除微信适配器：

```bash
# 1. 删除适配器文件
rm ~/.hermes/hermes-agent/gateway/platforms/weixin.py

# 2. 移除 config.yaml 中的 weixin 相关配置
# （手动编辑或让 Agent 帮忙）

# 3. 重启网关
```

---

## 技术架构说明

### 消息流
```
微信 ←→ iLink API ←→ weixin.py (长轮询) ←→ Hermes Gateway ←→ AI Agent
```

### 核心功能
- **消息接收**: Long-polling getUpdates 循环（35s 超时）
- **消息发送**: sendMessage API + context_token 回显
- **媒体上传**: AES-128-ECB 加密 → CDN 上传 → 发送引用
- **图片下载**: 下载并缓存到本地
- **语音转文字**: 使用微信内置的 voice_item.text
- **引用消息**: 提取 ref_msg 上下文拼接
- **去重**: 基于 message_id 的 5 分钟窗口去重
- **权限控制**: open / allowlist / pairing 三种 DM 策略

### 协议常量
- Base URL: `https://ilinkai.weixin.qq.com`
- CDN URL: `https://novac2c.cdn.weixin.qq.com/c2c`
- Channel Version: `2.1.3`
- Poll Timeout: 35000ms
- Max Consecutive Failures: 3（之后 backoff 30s）

---

*最后更新: 2026-04-09*
*适配器版本: v2.1.3 (兼容 Hermes Agent latest)*
