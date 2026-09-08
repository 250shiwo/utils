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
python kimi_watchdog.py <percent> <time> [--test-notify] [--test-delete] [--config CONFIG]
```

| 参数 | 说明 |
|---|---|
| `percent` | 周用量百分比阈值，如 `80` 表示用量达 80% 触发 |
| `time` | 目标时刻：`18:00`（今天，已过则明天）或 `2026-09-03 18:00` |
| `--test-notify` | 仅发送测试通知，验证渠道配置，不监控 |
| `--test-delete` | 删除链路干跑：刷新+匹配并打印将删除的 Key，但不真正删除 |
| `--config` | 配置文件路径，默认 `config.json` |

## 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 正常退出（测试通知/干跑成功 / Ctrl+C） |
| 1 | 额度阈值触发 |
| 2 | 指定时刻触发 |
| 3 | 监控异常（API 连续 5 次失败）；测试通知/干跑失败 |
| 4 | 额度阈值触发，但删除链路失败 |

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
| `refresh_token` | 空 | 网页版 localStorage 的 refresh_token；配置后额度触发时自动删除 api_key 对应的 Key |

## 自动删除 API Key（可选）

配置 `refresh_token` 后，当周用量达到阈值触发时，脚本会自动删除 `api_key`
对应的那把 Key（其他 Key 不受影响），然后通知并退出。

获取 `refresh_token`（签发后 90 天有效）：

1. 浏览器登录 `https://www.kimi.com/code/console`
2. F12 → Application（应用）→ Local Storage → `https://www.kimi.com`
3. 复制 `refresh_token` 键的值，填入 config.json

行为说明：

- 每次触发时脚本会先用 refresh_token 换新 access_token，并把轮换出的新
  refresh_token 自动写回 config.json（90 天有效期滚动续期，每次运行至多写一次）
- 删除失败（如 refresh_token 失效）会以退出码 4 退出，并在通知中说明原因；
  看到「重新抓取」提示时按上面步骤重新获取即可
- 时间触发不删除 Key；未配置 refresh_token 时脚本行为与之前完全一致
- 配置好后可运行 `python kimi_watchdog.py --test-delete` 干跑自检
  （打印将删除的 Key，但不真正删除）。注意：干跑会消耗一次 refresh_token
  轮换且不写回 config（实测旧值仍可用，无需处理）。

## 运行测试

```
python -m unittest tests.test_kimi_watchdog -v
```
