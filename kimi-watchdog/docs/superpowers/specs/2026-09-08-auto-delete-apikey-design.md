# 额度触发自动删除 API Key 设计文档

日期：2026-09-08
状态：已确认（用户已批准）
前置文档：`2026-09-03-kimi-watchdog-design.md`（监控脚本主体设计）

## 1. 背景与目标

kimi-watchdog 目前在周用量达阈值时仅发送 Server酱 通知并退出，后续处理需要人工介入。
本次改动让脚本在**额度阈值触发时自动删除 `config.json` 中 `api_key` 对应的那把 Key**，实现无人值守。

明确的行为边界（已与用户确认）：

- **仅额度阈值触发时删除**；到达指定时刻触发时不删，只通知。
- **只删 config 里 `api_key` 对应的那一把**（账号下其他工具的 Key 不动）。
- 删除后**通知并退出**，不自动创建新 Key。
- 功能可选：`refresh_token` 留空则删除功能关闭，监控行为与现状完全一致（向后兼容）。

## 2. 已实测验证的接口事实（2026-09-08）

以下为对用户账号实测（只读）确认的事实，均非官方接口，随时可能变更：

**认证体系**（网页版，与 `sk-` API Key 不同体系）：

- `access_token`：JWT，Bearer 使用。登录签发的仅 15 分钟；**刷新接口签发的为 30 天**。
- `refresh_token`：JWT（`typ:"refresh"`），存于浏览器 localStorage，**90 天有效**。
  每次调用刷新接口都会**轮换**（响应里返回新 refresh_token；实测旧值短期内仍可用）。

**刷新接口**：

```
GET https://www.kimi.com/api/auth/token/refresh
Authorization: Bearer <refresh_token>
→ 200 {"access_token": "...", "refresh_token": "..."}   # 两个都是新签发的
→ 401 {"error_type":"auth.token.invalid",...}            # refresh_token 失效
```

**列出 Key**（Connect RPC 风格，POST 即查询）：

```
POST https://www.kimi.com/apiv2/kimi.gateway.credentials.v1.APIKeyService/ListAPIKeys
Authorization: Bearer <access_token>
Content-Type: application/json
Connect-Protocol-Version: 1
{"page_size":100,"scope":["FEATURE_CODING"]}
→ 200 {"apiKeys":[{"key":"sk-ki...dSFze","name":"配额查询","status":"STATUS_ACTIVE",
                    "id":"1a0667b8-...","createTime":"...","updateTime":"..."}, ...]}
```

注意：`key` 字段是**掩码**（前缀 `sk-ki` + `...` + 后 5 位），不是完整 Key。

**删除 Key**：

```
POST https://www.kimi.com/apiv2/kimi.gateway.credentials.v1.APIKeyService/DeleteAPIKey
（请求头同 ListAPIKeys）
{"id":"1a0667b8-..."}
→ 200 即成功（响应体约 26 字节，内容不解析）
```

未实测项：DeleteAPIKey 由本脚本真实调用（破坏性操作，留给真实触发；该接口格式已被用户
抓包时的真实删除验证过）。

## 3. 方案选型

**方案 A（已选定）：触发时一次性刷新**

config 存 `refresh_token`（90 天）；仅在触发那一刻执行 刷新 → List 匹配 → Delete。
不写回 config，无后台保活，无额外状态。

被否决的方案：

- B（A + 刷新后写回 config 保活）：写文件有竞态/损坏风险，收益小。
- C（config 直接存 key_id 跳过 List）：key 重建后需手动改 config，且 ID 只能抓包看到，不够自动化。

## 4. 详细设计

### 4.1 配置

`config.json` 新增键（`config.example.json` 同步加占位）：

| 键 | 默认值 | 说明 |
|---|---|---|
| `refresh_token` | 空字符串 | 网页版 localStorage 里的 `refresh_token`，90 天有效；空 = 删除功能关闭 |

`config.json` 已被 `.gitignore` 忽略，凭证不会误提交。

启动自检（仅当配置了 `refresh_token`）：本地解码 JWT 的 `exp`，不发网络请求——

- 已过期：控制台警告「refresh_token 已过期，删除功能将不可用」；
- 剩余 < 7 天：控制台提醒重新抓取；
- 两种情况都**不阻断监控**（删除功能失效不应拖垮监控本体）。

### 4.2 新增函数（kimi_watchdog.py）

```python
def refresh_access_token(refresh_token):
    """GET /api/auth/token/refresh，Bearer 认证。
    返回新的 access_token；HTTP 非 200 / 网络错误抛异常（401 单独标识）。"""

def list_api_keys(access_token):
    """POST ListAPIKeys（scope=FEATURE_CODING），返回 apiKeys 列表。"""

def find_key_id(api_keys, api_key):
    """把 config 的完整 api_key 与列表中的掩码 key 匹配。
    掩码形如 'sk-ki...dSFze'：按 '...' 拆前缀/后缀，
    api_key 同时满足 startswith(前缀) 和 endswith(后缀) 即视为同一把。
    恰好 1 个匹配 -> 返回 (id, name)；0 个 -> 返回 None；>1 个 -> 抛异常（安全起见拒绝删除）。"""

def delete_api_key(access_token, key_id):
    """POST DeleteAPIKey {"id": key_id}，HTTP 200 即成功，否则抛异常。"""
```

### 4.3 主流程集成（仅改动额度触发分支，时间触发分支完全不动）

`pct >= 阈值` 且配置了 `refresh_token` 时，按序执行：

1. 构建原有用量通知内容；
2. 执行删除链路（刷新 → 列表 → 匹配 → 删除）；
3. 把删除结果（成功 / 失败原因）追加到通知正文；
4. Server酱 发送，按下方退出码退出。

错误处理矩阵：

| 环节 | 故障 | 通知内容 | 退出码 |
|---|---|---|---|
| 刷新 | 401 | 「refresh_token 已失效，请重新登录 kimi.com 后从 localStorage 重新抓取」 | 4 |
| 刷新/列表/删除 | 网络异常、非 200 | 通知中附异常信息 | 4 |
| 匹配 | 0 个匹配 | 「未找到匹配的 Key，可能已被删除，无需处理」 | 1 |
| 匹配 | 多个匹配 | 「掩码后缀撞车匹配到多把 Key，为安全起见未删除」 | 4 |
| 删除 | 成功 | 「已删除 Key：name=xx id=xx」 | 1 |

### 4.4 退出码（在现有约定上新增 4）

| 退出码 | 含义 |
|---|---|
| 0 | 正常退出（测试通知/测试删除成功、Ctrl+C） |
| 1 | 额度阈值触发（未启用删除，或删除成功/无需删除） |
| 2 | 指定时刻触发 |
| 3 | 监控异常（API 连续 5 次失败） |
| 4 | 额度阈值触发，但删除链路失败（新增） |

### 4.5 干跑自检 `--test-delete`

配置后自检用：执行刷新 + 列表 + 匹配，打印「将删除 name=xx id=xx」但**不调用删除接口**。
与 `--test-notify` 一样不需要位置参数。成功返回 0，失败返回 3。
注意：每次运行都会消耗一次 refresh_token 轮换（实测旧值仍可用，无副作用）。

## 5. 测试计划

沿用现有 `unittest + mock` 风格（零依赖），新增覆盖：

- `refresh_access_token`：200 返回 token；401/500/网络异常抛出；
- `list_api_keys`：正常解析；非 200 抛出；
- `find_key_id`：恰好 1 个匹配 / 0 匹配返回 None / 多匹配抛异常 / 掩码格式异常跳过；
- `delete_api_key`：200 成功；非 200 抛出；
- 主循环额度触发分支：删除成功（exit 1）/ 刷新 401（exit 4）/ 未配置 refresh_token（exit 1，行为同现状）/ 0 匹配（exit 1）；
- `--test-delete` 模式：mock 三函数，验证不调用删除、退出码正确；
- 启动自检：过期 / 临期 refresh_token 的警告输出（不阻断）。

回归：现有测试套件全部保持绿色。

## 6. 文档与收尾

- README：`refresh_token` 抓取方法（F12 → Application → Local Storage → kimi.com）、
  退出码 4、`--test-delete` 用法；
- 模块 docstring 更新退出码约定与功能描述；
- 删除一次性验证脚本 `verify_refresh.py`；
- 把当前最新的 refresh_token 写入用户本机 `config.json`。

## 7. 明确不做（YAGNI）

- 自动创建新 Key（用户明确只要删除）；
- 把轮换出的新 refresh_token 写回 config / 后台保活；
- 删除账号下全部 Key 或按名称删除；
- DeleteAPIKey 的真实调用测试（破坏性，由真实触发验证）。
