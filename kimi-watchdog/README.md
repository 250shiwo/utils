# kimi-watchdog

Kimi Code API 周额度监控脚本：当本周用量达到设定阈值，或到达指定时刻时，
通过 **Server酱（微信推送）** 发送提醒通知，然后退出。

零依赖，仅需 Python 3.8+（Server酱是纯 HTTP 接口，无需安装 SDK）。

## 快速开始

1. 复制配置模板并填写：

   ```
   copy config.example.json config.json
   ```

   - `api_key`：Kimi Code 的 API Key（也可用环境变量 `KIMI_API_KEY`，优先级更高）
   - `serverchan_sendkey`：[Server酱](https://sct.ftqq.com/) 的 SendKey

2. 测试通知渠道连通性：

   ```
   python kimi_watchdog.py --test-notify
   ```

3. 启动监控（周用量达 80% 或今天 18:00 触发，任一先到即通知并退出）：

   ```
   python kimi_watchdog.py 80 18:00
   ```

## 参数说明

```
python kimi_watchdog.py <percent> <time> [--test-notify] [--config CONFIG]
```

| 参数 | 说明 |
|---|---|
| `percent` | 周用量百分比阈值，如 `80` 表示用量达 80% 触发 |
| `time` | 目标时刻：`18:00`（今天，已过则明天）或 `2026-09-03 18:00` |
| `--test-notify` | 仅发送测试通知，验证渠道配置，不监控 |
| `--config` | 配置文件路径，默认 `config.json` |

## 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 正常退出（测试通知成功 / Ctrl+C） |
| 1 | 额度阈值触发 |
| 2 | 指定时刻触发 |
| 3 | 监控异常（API 连续 5 次失败） |

## 数据来源

调用 `GET https://api.kimi.com/coding/v1/usages`（社区逆向的非官方接口，
官方随时可能变更）。注意它与 `api.moonshot.cn` 开放平台余额接口是不同的体系，
API Key 不通用。

## 配置项（config.json）

| 键 | 默认值 | 说明 |
|---|---|---|
| `api_key` | 空 | Kimi Code API Key |
| `poll_interval_sec` | 600 | 轮询间隔（秒） |
| `serverchan_sendkey` | 空 | Server酱 SendKey |

## 运行测试

```
python -m unittest tests.test_kimi_watchdog -v
```
