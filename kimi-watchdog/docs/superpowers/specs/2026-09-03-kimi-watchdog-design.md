# Kimi Code 额度监控脚本（kimi-watchdog）设计文档

日期：2026-09-03
状态：已确认

## 1. 背景与目标

Kimi Code（Kimi 编程订阅）提供每周 API 额度，额度用尽会影响开发工作。本项目开发一个监控脚本，当**本周用量达到设定阈值**或**到达指定时刻**时，通过 **Server酱（微信推送）** 发送提醒通知，然后退出。

## 2. 数据来源

调用非官方接口（社区逆向工程，随时可能变更）：

- `GET https://api.kimi.com/coding/v1/usages`
- 认证：请求头 `Authorization: Bearer <KIMI_API_KEY>`
- 响应包含：
  - `usage`：周配额（`limit` 总量、`remaining` 剩余、`resetTime` 重置时间）
  - `limits[]`：5 小时滚动窗口（300 分钟）的配额明细
  - `user.membership.level`：会员等级

注意：该接口与 `api.moonshot.cn/v1/users/me/balance`（开放平台余额）是不同的体系，API Key 不通用。

## 3. 技术选型

**方案 A（已选定）：单文件 Python 零依赖脚本**

- `kimi_watchdog.py`：全部逻辑，约 300 行，带详细中文注释
- `config.json`：配置文件
- 仅使用标准库：`urllib.request`（HTTP，Server酱推送同样用它）、`json`、`argparse`、`time`、`datetime`
- 要求 Python 3.8+，无需 pip 安装任何依赖

## 4. 用法

```
python kimi_watchdog.py <percent> <time> [--test-notify]
```

- `percent`：周用量百分比阈值。如 `80` 表示本周用量达到 80% 时触发。
- `time`：绝对时刻。`18:00` 表示今天 18:00（启动时已过则视为明天同一时刻）；也支持完整格式 `2026-09-03 18:00`。
- `--test-notify`：可选，跳过监控逻辑，仅向两个渠道各发一条测试通知，用于验证配置连通性。**此模式下 `percent` 和 `time` 不必提供**；若在通知中需要引用用量数据，测试通知仅发送固定文案。
- 两个位置参数在监控模式（默认）下均为必填。

**任一条件满足 → 双渠道通知 → 脚本退出。**

## 5. 退出码约定

| 退出码 | 含义 |
|---|---|
| 0 | 正常退出（含 `--test-notify` 成功、Ctrl+C 主动停止） |
| 1 | 额度阈值触发 |
| 2 | 指定时刻触发 |
| 3 | 监控异常（API 连续 5 次失败） |

## 6. 配置文件 `config.json`

```json
{
  "api_key": "sk-...",
  "poll_interval_sec": 600,
  "serverchan_sendkey": "SCT..."
}
```

- `api_key` 也可通过环境变量 `KIMI_API_KEY` 提供，**环境变量优先**于配置文件。
- `poll_interval_sec`：轮询间隔秒数，默认 600（10 分钟）。
- `serverchan_sendkey` 未配置时脚本报错退出。
- Server酱**无需安装任何 SDK**：它就是一个 HTTP POST 接口（`https://sctapi.ftqq.com/<sendkey>.send`），标准库 `urllib` 直接调用，保持零依赖。

## 7. 核心流程

1. **启动**：解析命令行参数（`argparse`）→ 加载 `config.json` → 校验配置。
2. **立即首次检查**：不等第一个轮询周期，启动后立即查询一次。
3. **主循环**：
   - 调用 usages API，解析 JSON；
   - 计算周用量百分比：`used% = (limit - remaining) / limit × 100`；
   - 判断触发条件：`used% ≥ percent` 或 `当前时间 ≥ 目标时刻`；
   - 未触发则 `sleep(poll_interval_sec)` 后继续。
4. **触发与通知**：
   - 组装消息，包含：触发原因、本周用量（`已用/总量 (百分比)`）、剩余额度、5 小时窗口剩余、重置时间、会员等级；
   - 调用 Server酱 推送（单次 HTTP POST）；
   - 控制台打印发送结果与触发原因，按退出码约定退出。

## 8. 错误处理

- **API 请求失败**（网络错误、超时 10s、非 200 状态码、JSON 解析失败）：打印警告，本轮跳过，下轮重试；**连续 5 次失败**则向可用渠道发送"监控异常"通知后以退出码 3 退出；连续失败计数在成功一次后清零。
- **周配额重置**：轮询期间若 `resetTime` 已过、百分比回落，属正常现象，继续监控，不做特殊处理。
- **通知发送失败**：Server酱 返回非 0 `code` 或 HTTP 错误时打印错误详情；监控模式下发送失败不改变触发退出码，`--test-notify` 模式下发送失败返回退出码 3。
- **Ctrl+C**：捕获 `KeyboardInterrupt`，打印提示后以退出码 0 退出。

## 9. 模块划分（文件内部函数级）

| 函数 | 职责 |
|---|---|
| `load_config()` | 读取并校验配置，合并环境变量 |
| `parse_deadline(time_str)` | 解析时间参数为 `datetime`，处理"今天已过则顺延明天" |
| `fetch_usage(api_key)` | 调用 usages API，返回解析后的用量字典 |
| `compute_used_percent(usage)` | 计算周用量百分比 |
| `send_serverchan(sendkey, title, body)` | Server酱推送 |
| `build_message(reason, usage)` | 组装通知内容 |
| `main()` | 参数解析与主循环 |

每个函数可通过 `--test-notify` 或直接调用独立验证，无需复杂测试框架。

## 10. 范围外（YAGNI）

- 不做 Windows 服务/开机自启封装（用户可自行用任务计划程序或 `pythonw` 挂后台）。
- 不做多账户监控。
- 不做用量历史记录/图表。
- 不做轮询间隔外的重试退避策略。
