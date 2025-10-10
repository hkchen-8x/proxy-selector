# Proxy Manager 脚本目录

## 目录结构

```
scripts/
├── proxy_manager.py       # 主入口文件
├── proxy_manager.old.py   # 旧版本备份（单文件）
└── modules/               # 功能模块
    ├── __init__.py        # 模块导出（延迟加载）
    ├── config.py          # 配置管理
    ├── state.py           # 状态管理
    ├── probe.py           # Playwright 探测
    ├── xray_client.py     # Xray API 客户端
    └── notifier.py        # Telegram 通知
```

## 模块说明

### config.py - 配置管理
**职责**：配置文件解析和数据结构定义

**主要类**：
- `AppConfig`: 应用配置
- `Probe`: 探测点配置
- `Expectation`: 期望条件
- `ConfigLoader`: 配置加载器

**依赖**：标准库

### state.py - 状态管理
**职责**：记录和查询探测状态

**主要类**：
- `ProbeState`: 探测状态数据类
- `StateManager`: 状态管理器

**功能**：
- 状态持久化到 JSON
- 次优解跳过判断
- 时间计算

**依赖**：标准库

### probe.py - Playwright 探测
**职责**：执行浏览器探测和质量检测

**主要类**：
- `PlaywrightProbe`: Playwright 探测器
- `ProbeOutcome`: 探测结果

**功能**：
- 无头浏览器访问
- 质量等级检测（optimal/suboptimal/blocked）
- must_not 禁止特征检测
- fallback_expect 次优解验证

**依赖**：playwright

### xray_client.py - Xray API 客户端
**职责**：与 Xray API 交互

**主要类**：
- `XrayAPIClient`: API 客户端
- `XrayAPIError`: API 错误

**功能**：
- 添加路由规则
- 删除路由规则
- Dry-run 模式

**依赖**：标准库（subprocess）

### notifier.py - Telegram 通知
**职责**：发送 Telegram 告警

**主要类**：
- `TelegramNotifier`: 通知发送器

**功能**：
- 异步发送消息
- HTML 格式支持
- 错误处理

**依赖**：aiohttp（可选）

### proxy_manager.py - 主入口
**职责**：编排各模块，实现整体流程

**主要类**：
- `ProbeManager`: 探测管理器

**流程**：
1. 加载配置
2. 执行探测
3. 判断质量等级
4. 处理故障（切换出站）
5. 发送告警
6. 更新状态

## 使用方法

### 基本运行

```bash
python3 proxy_manager.py --config /path/to/config.json
```

### 导入模块

```python
from modules import ConfigLoader, StateManager, PlaywrightProbe

# 加载配置
config = ConfigLoader.load(Path("config.json"))

# 状态管理
state = StateManager(Path("state.json"))

# 探测（需要安装 playwright）
probe = PlaywrightProbe(timeout_ms=20000)
```

## 开发指南

### 添加新模块

1. 在 `modules/` 下创建新文件
2. 实现功能类
3. 在 `modules/__init__.py` 中添加延迟导入逻辑
4. 更新 `__all__` 列表

### 修改模块

- **单一职责**：每个模块专注一个领域
- **最小依赖**：避免模块间循环依赖
- **清晰接口**：通过数据类传递数据
- **错误处理**：定义专用异常类

### 测试模块

```bash
# 测试配置加载
python3 -c "from modules.config import ConfigLoader; print('OK')"

# 测试状态管理
python3 -c "from modules.state import StateManager; print('OK')"

# 测试 Xray 客户端
python3 -c "from modules.xray_client import XrayAPIClient; print('OK')"
```

## 代码规范

- **类型提示**：使用 type hints
- **文档字符串**：添加模块和类说明
- **异常处理**：捕获具体异常
- **日志记录**：使用 logging 模块
- **命名规范**：遵循 PEP 8

## 依赖关系

```
proxy_manager.py
  ├─> modules.config (无外部依赖)
  ├─> modules.state (无外部依赖)
  ├─> modules.probe (需要 playwright)
  ├─> modules.xray_client (无外部依赖)
  └─> modules.notifier (需要 aiohttp)
```

**最小依赖运行**：
- 只加载配置：不需要任何外部依赖
- 完整运行：需要 playwright 和 aiohttp

## 版本历史

- **v2.0**: 模块化重构，删除 cookie 功能
- **v1.0**: 状态管理与智能质量检测
- **v0.1**: 初始版本（单文件）

