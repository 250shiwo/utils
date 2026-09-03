# utils

个人小工具集合仓库（monorepo）：每个工具一个子目录，自带 README 与测试，互不依赖。

## 工具列表

| 工具 | 说明 |
|---|---|
| [kimi-watchdog](./kimi-watchdog) | Kimi Code API 周额度监控：用量达阈值或到指定时刻时，通过 Server酱 微信推送提醒 |

## 添加新工具

1. 在仓库根目录新建子目录（kebab-case 命名，如 `my-new-tool/`）
2. 工具代码、README、测试放在自己的子目录内
3. 在上方表格登记一行简介
