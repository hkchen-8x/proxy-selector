# Proxy Manager - Xray 代理自动切换工具

## 功能简介

通过 Playwright 对目标站点进行健康拨测，智能区分三种质量等级：
- **最优解**: 无需人机验证，直接访问成功
- **次优解**: 有人机验证（Cloudflare、reCAPTCHA 等），但可通过
- **最差解**: 完全被禁止访问

当检测到代理失效（最差解）时，自动通过 Xray API 切换到可用的出站代理，**优先寻找最优解**。通过 Telegram 发送不同级别的告警通知。

## 安装依赖

```bash
# 安装 Python 依赖
pip install -r requirements.txt

# 安装 Chromium 浏览器
playwright install chromium
```

## 配置说明

### 基础配置 (config.json)

```json
{
  "proxy": {
    "prod": "socks5://127.0.0.1:7890",
    "test": "socks5://127.0.0.1:7891"
  },

  "playwright_probes": [{
    "name": "buyee",
    "url": "https://buyee.jp",
    "expect": {
      "status": 200,
      "title": "Buyee",
      "body": "buyee.jp",
      "captcha_keywords": ["challenge", "captcha", "cf-"],
      "block_keywords": ["403 Forbidden", "Access Denied", "banned"]
    },
    "outbounds": {
      "candidates": ["proxy-1", "proxy-2"],
      "replace": false
    },
    "rules": {
      "domain": ["domain:buyee.jp"]
    },
    "cookie_file": "cookies/buyee.json"
  }],

  "default_outbounds": [
    "freedom",
    "IPv4",
    "http-proxy-resiprox"
  ],
  
  "xray": {
    "api": "127.0.0.1:8080",
    "exe": "xray"
  },
  
  "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
  
  "telegram": {
    "bot_token": "YOUR_BOT_TOKEN",
    "chat_id": "YOUR_CHAT_ID",
    "enabled": true
  }
}
```

### Telegram 配置

#### 1. 创建 Telegram Bot

1. 在 Telegram 中找到 [@BotFather](https://t.me/botfather)
2. 发送 `/newbot` 创建新机器人
3. 按提示设置机器人名称和用户名
4. 获得 `bot_token`（格式：`123456789:ABCdefGHIjklMNOpqrsTUVwxyz`）

#### 2. 获取 Chat ID

方法一：通过 [@userinfobot](https://t.me/userinfobot)
- 向机器人发送任意消息，它会回复你的 `chat_id`

方法二：通过 API
```bash
# 先向你的 bot 发送一条消息，然后执行：
curl https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
```

#### 3. 在配置文件中添加

```json
"telegram": {
  "bot_token": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
  "chat_id": "987654321",
  "enabled": true
}
```

- `bot_token`: 从 BotFather 获得的令牌
- `chat_id`: 你的用户 ID 或群组 ID（群组 ID 以 `-` 开头）
- `enabled`: 是否启用告警（可临时关闭）

## 使用方法

### 基本运行

```bash
python3 scripts/proxy_manager.py --config config.json
```

### 命令行参数

```bash
python3 scripts/proxy_manager.py \
  --config config.json \
  --log-file logs/proxy_manager.log \
  --timeout 20000 \
  --verbose \
  --dry-run
```

- `--config`: 配置文件路径（默认：`config.json`）
- `--log-file`: 日志文件路径（默认：`logs/proxy_manager.log`）
- `--timeout`: 页面加载超时时间，单位毫秒（默认：20000）
- `--dry-run`: 仅打印命令不实际执行 Xray API 调用
- `--verbose`: 输出详细调试日志

### 定时任务

使用 cron 定期运行：

```bash
# 每 5 分钟检查一次
*/5 * * * * cd /root/proxy/proxy-manager && /usr/bin/python3 scripts/proxy_manager.py --config config.json >> logs/cron.log 2>&1
```

## 告警示例

### 质量降级提醒（次优解）

```
⚠️ 代理质量降级提醒

📍 站点: buyee
🌐 URL: https://buyee.jp
⚡ 状态: 次优解 (有人机验证)
📝 详情: 检测到人机验证，可能需要交互
🕐 时间: 2025-10-10 15:30:45

当前可访问但需要通过人机验证，建议关注是否影响自动化流程。
```

### 成功切换出站

```
🔄 代理出站已自动切换

📍 站点: buyee
🌐 URL: https://buyee.jp
✅ 新出站: proxy-2
🕐 时间: 2025-10-10 15:30:45

原出站拨测失败，已自动切换到可用出站。
```

### 切换失败（需人工介入）

```
⚠️ 代理出站切换失败 - 需人工介入

📍 站点: buyee
🌐 URL: https://buyee.jp
❌ 状态: 所有候选出站均不可用
🕐 时间: 2025-10-10 15:30:45

请尽快检查网络状态和出站配置！
```

## 工作流程

1. **拨测阶段**：
   - 使用 `prod` 代理通过 Playwright 访问目标站点

2. **智能质量检测**：
   - **最优解**: 无验证码，页面正常 → 保持现状
   - **次优解**: 检测到人机验证特征（Cloudflare、reCAPTCHA 等）→ 记录但不切换，发送提醒
   - **最差解**: 满足 must_not 条件 → 触发出站切换

3. **故障恢复**（仅最差解）：
   - 依次尝试候选出站，使用 `test` 代理测试
   - **优先寻找最优解**（无验证码）
   - 如果找不到最优解，接受次优解（有验证码但可用）
   - 找到可用出站后切换生产规则

4. **Telegram 告警**：
   - 次优解：发送提醒通知
   - 成功切换：发送通知说明新出站和质量等级
   - 全部失败：发送紧急告警要求人工介入

## 日志记录

所有操作记录到日志文件：
- 拨测结果（成功/失败）
- 出站切换操作
- Xray API 调用
- Telegram 告警发送状态

查看日志：
```bash
tail -f logs/proxy_manager.log
```

## 故障排查

### Telegram 告警不工作

1. 检查 `bot_token` 和 `chat_id` 是否正确
2. 确认已安装 `aiohttp`：`pip install aiohttp`
3. 检查日志中的错误信息
4. 测试 bot 是否能正常接收消息

### Xray API 调用失败

1. 确认 Xray API 地址和端口正确
2. 确认 Xray 已启动并开启 API
3. 使用 `--dry-run` 查看实际命令
4. 手动执行命令测试

### 拨测总是失败

1. 检查代理配置是否正确
2. 使用 `--verbose` 查看详细日志
3. 检查超时时间设置（`--timeout`）
4. 确认目标站点可访问

## 高级功能

### 状态管理与智能跳过

系统会自动记录每个探测点的状态到 `state.json`：

```json
{
  "buyee": {
    "probe_name": "buyee",
    "quality": "suboptimal",
    "outbound": null,
    "last_check_time": "2025-10-10T15:30:45.123456",
    "reason": "检测到人机验证特征: cf-challenge"
  }
}
```

**次优解智能跳过**：
- 当探测点处于次优解状态（有人机验证但可用）
- 在配置的时间内（默认 1 小时）会跳过探测，避免频繁触发
- 超过时间阈值后，尝试寻找最优解

配置方式：
```json
{
  "state_file": "state.json",
  "suboptimal_skip_hours": 1
}
```

- `state_file`: 状态文件路径（默认 `state.json`）
- `suboptimal_skip_hours`: 次优解跳过探测的小时数（默认 1）

**优势**：
- 减少对目标站点的请求频率
- 避免频繁触发 Cloudflare 等风控
- 超过阈值后重新寻找最优解，避免长期停留在次优状态

### 质量等级检测（必看）

使用 `fallback_expect` 和 `must_not` 精确控制质量判断：

```json
{
  "expect": {
    "status": 200,
    "title": "Buyee",
    "body": "buyee.jp",
    
    "captcha_keywords": [
      "cf-challenge",      // Cloudflare 挑战
      "g-recaptcha",       // Google reCAPTCHA
      "hcaptcha",          // hCaptcha
      "challenge-platform"
    ],
    
    "fallback_expect": {
      "status": 403,
      "title": "Just a moment"
    },
    
    "must_not": {
      "status": [451],
      "title": ["banned", "forbidden"],
      "body": ["Access Denied", "Your IP has been blocked"]
    }
  }
}
```

**工作原理**：
1. **检查 must_not（禁止特征）** → 最差解（触发切换）
   - 匹配任意禁止状态码、标题或内容关键字
   - **注意**: 403 不再默认为禁止，需在 `must_not.status` 中显式配置

2. **检查 captcha_keywords（人机验证）** → 次优解
   - 检测到验证码特征
   - 如果配置了 `fallback_expect`，必须满足才算次优解
   - 不满足 `fallback_expect` 则降级为最差解

3. **验证基础 expect** → 最优解
   - 无验证码且满足所有期望

**示例场景**：
- Cloudflare 403 挑战页面 → 满足 `fallback_expect` → 次优解 ✅
- 真正的 403 禁止页面 → 在 `must_not.body` 中匹配到 "Access Denied" → 最差解 ❌
- 正常访问 200 页面 → 最优解 ✅

### 自定义路由规则

在 probe 配置中添加 `rules` 字段：

```json
{
  "name": "example",
  "url": "https://example.com",
  "rules": {
    "domain": ["domain:example.com"],
    "network": "tcp",
    "port": "443",
    "protocol": ["http", "tls"]
  }
}
```

### 自定义 User-Agent

在配置文件中设置全局 UA：
```json
{
  "user_agent": "Mozilla/5.0 ..."
}
```

## 许可证

MIT License

