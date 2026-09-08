# 额度触发自动删除 API Key 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** kimi-watchdog 在周用量阈值触发时，自动删除 config 中 `api_key` 对应的那把 Kimi Code API Key（刷新网页版 token → 列表匹配掩码 → 删除 → 通知 → 退出），并支持 `--test-delete` 干跑自检。

**Architecture:** 全部改动落在现有单文件脚本 `kimi_watchdog.py`（零依赖标准库风格），新增 5 个职责单一的函数；主循环仅改动额度触发分支；触发成功刷新后把轮换的新 `refresh_token` 原子写回 config（每次运行至多写一次）。

**Tech Stack:** Python 3.8+ 标准库（urllib/json/argparse/base64/tempfile），unittest + mock 测试。

设计文档（含已实测的接口格式与错误矩阵）：`docs/superpowers/specs/2026-09-08-auto-delete-apikey-design.md`

## Global Constraints

- 零第三方依赖，仅用 Python 标准库；要求 Python 3.8+。
- 测试框架为标准库 unittest，运行命令：`python -m unittest tests.test_kimi_watchdog -v`；单类运行：`python -m unittest tests.test_kimi_watchdog.<类名> -v`。
- 代码注释、提交信息使用中文；提交信息用 conventional 前缀（`feat:` / `test:` / `docs:` / `chore:`，参照 git log）。
- **安全红线：任何真实凭证（api_key、refresh_token、sendkey）不得写入代码、测试、计划或 git 提交。`config.json` 已被 .gitignore 忽略，永不 `git add` 它。**
- 除 Task 8 指定的 `verify_refresh.py` 删除外，不新建任何计划外文件。
- 环境：Windows + Git Bash；命令均在项目根目录 `kimi-watchdog/` 下执行。

## 接口契约（各任务之间靠这里对齐）

```python
# kimi_watchdog.py 新增常量
REFRESH_URL = "https://www.kimi.com/api/auth/token/refresh"
LIST_KEYS_URL = "https://www.kimi.com/apiv2/kimi.gateway.credentials.v1.APIKeyService/ListAPIKeys"
DELETE_KEY_URL = "https://www.kimi.com/apiv2/kimi.gateway.credentials.v1.APIKeyService/DeleteAPIKey"
EXIT_DELETE_FAILED = 4

# 新增函数签名
def refresh_access_token(refresh_token): ...   # -> (access_token, new_refresh_token)；失败抛 RuntimeError，HTTP 错误带 .status 属性
def save_refresh_token(config_path, new_refresh_token): ...  # -> None；读-改-写原子替换；IO 错误抛异常
def list_api_keys(access_token): ...           # -> list[dict]（apiKeys 数组）；失败抛 RuntimeError
def find_key_id(api_keys, api_key): ...        # -> (id, name) | None；多匹配抛 RuntimeError
def delete_api_key(access_token, key_id): ...  # -> None；非 200 抛 RuntimeError
def check_refresh_token_expiry(refresh_token): ...  # -> float（剩余天数，负数=已过期）| None（无法解析）

# 主循环内部辅助（不对外）
def _attempt_key_deletion(cfg, config_path): ...  # -> (ok: bool, detail: str)
```

---

### Task 1: `refresh_access_token()` 刷新接口

**Files:**
- Modify: `kimi_watchdog.py`
- Test: `tests/test_kimi_watchdog.py`

**Interfaces:**
- Consumes: 无（首个任务）
- Produces: `REFRESH_URL`、`refresh_access_token(refresh_token) -> (access_token, new_refresh_token)`；HTTP 失败抛 `RuntimeError` 且异常对象带 `.status` 属性（Task 5 用它识别 401）

- [ ] **Step 1: 写失败测试**

在 `tests/test_kimi_watchdog.py` 顶部 import 区加 `import urllib.error`，文件末尾（`if __name__ == "__main__":` 之前）追加：

```python
class TestRefreshAccessToken(unittest.TestCase):
    """refresh_access_token：用 refresh_token 换新 access_token"""

    def test_ok(self):
        """200：返回 (access_token, 新 refresh_token)，请求头带 Bearer refresh_token"""
        resp = _FakeResponse({"access_token": "at-1", "refresh_token": "rt-2"})
        with mock.patch.object(kw.urllib.request, "urlopen",
                               return_value=resp) as m:
            result = kw.refresh_access_token("rt-1")
        self.assertEqual(result, ("at-1", "rt-2"))
        req = m.call_args[0][0]
        self.assertEqual(req.headers["Authorization"], "Bearer rt-1")

    def test_http_error_carries_status(self):
        """401：抛 RuntimeError，且 .status == 401（供上层识别重新抓取）"""
        err = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
        with mock.patch.object(kw.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as cm:
                kw.refresh_access_token("rt-1")
        self.assertEqual(cm.exception.status, 401)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_kimi_watchdog.TestRefreshAccessToken -v`
Expected: FAIL（`AttributeError: module 'kimi_watchdog' has no attribute 'refresh_access_token'`）

- [ ] **Step 3: 实现**

`kimi_watchdog.py` 常量区（`SERVERCHAN_URL` 之后）追加：

```python
# 网页版控制台接口（非官方，逆向自 kimi.com 前端；与 sk- API Key 不同体系）
REFRESH_URL = "https://www.kimi.com/api/auth/token/refresh"
LIST_KEYS_URL = "https://www.kimi.com/apiv2/kimi.gateway.credentials.v1.APIKeyService/ListAPIKeys"
DELETE_KEY_URL = "https://www.kimi.com/apiv2/kimi.gateway.credentials.v1.APIKeyService/DeleteAPIKey"
```

退出码常量区（`EXIT_ERROR = 3` 之后）追加：

```python
EXIT_DELETE_FAILED = 4  # 额度触发但删除链路失败
```

`fetch_usage` 函数之后新增：

```python
# ==================== 网页版控制台接口（Key 删除链路） ====================
def refresh_access_token(refresh_token):
    """用 refresh_token 换新的 access_token（refresh_token 同时被轮换）。

    GET /api/auth/token/refresh，Bearer 认证。refresh_token 来自浏览器
    localStorage，签发后 90 天有效；本接口返回的新 refresh_token 重新计时 90 天。

    :param refresh_token: 网页版 refresh_token（JWT）
    :return: (access_token, new_refresh_token) 二元组
    :raises RuntimeError: HTTP 非 200（异常对象带 .status 属性，401=需重新抓取）
    :raises Exception: 网络错误、超时、JSON 解析失败等
    """
    req = urllib.request.Request(
        REFRESH_URL, headers={"Authorization": f"Bearer {refresh_token}"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                err = RuntimeError(f"刷新接口返回状态码 {resp.status}")
                err.status = resp.status
                raise err
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = RuntimeError(f"刷新接口返回状态码 {e.code}")
        err.status = e.code
        raise err
    return body["access_token"], body["refresh_token"]
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m unittest tests.test_kimi_watchdog.TestRefreshAccessToken -v`
Expected: 2 个测试 PASS

- [ ] **Step 5: 提交**

```bash
git add kimi_watchdog.py tests/test_kimi_watchdog.py
git commit -m "feat: 新增 refresh_access_token 用 refresh_token 换 access_token"
```

---

### Task 2: `save_refresh_token()` 原子写回 config

**Files:**
- Modify: `kimi_watchdog.py`
- Test: `tests/test_kimi_watchdog.py`

**Interfaces:**
- Consumes: 无
- Produces: `save_refresh_token(config_path, new_refresh_token) -> None`（Task 5 调用）；保留文件其余键，临时文件 + `os.replace` 原子替换

- [ ] **Step 1: 写失败测试**

`tests/test_kimi_watchdog.py` 追加：

```python
class TestSaveRefreshToken(unittest.TestCase):
    """save_refresh_token：把轮换出的新 refresh_token 写回配置文件"""

    def test_roundtrip_preserves_other_keys(self):
        """写回后其余配置键原样保留"""
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"api_key": "sk-x", "refresh_token": "old",
                       "poll_interval_sec": 300}, f)
        self.addCleanup(os.remove, path)
        kw.save_refresh_token(path, "new-rt")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["refresh_token"], "new-rt")
        self.assertEqual(data["api_key"], "sk-x")
        self.assertEqual(data["poll_interval_sec"], 300)

    def test_missing_file_raises(self):
        """目标文件不存在时抛异常（由上层兜底告警）"""
        with self.assertRaises(FileNotFoundError):
            kw.save_refresh_token("no_such_dir_9x7/no_such.json", "x")
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_kimi_watchdog.TestSaveRefreshToken -v`
Expected: FAIL（`AttributeError: ... no attribute 'save_refresh_token'`）

- [ ] **Step 3: 实现**

`kimi_watchdog.py` import 区加 `import tempfile`。`refresh_access_token` 之后新增：

```python
def save_refresh_token(config_path, new_refresh_token):
    """把轮换出的新 refresh_token 写回配置文件（读-改-写，其余键原样保留）。

    先写同目录临时文件再 os.replace 原子替换，避免写一半损坏 config。
    每次额度触发至多调用一次，无并发写场景。

    :param config_path: 配置文件路径
    :param new_refresh_token: 刷新接口返回的新 refresh_token
    :raises Exception: 文件不存在、无权限、磁盘错误等
    """
    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)
    data["refresh_token"] = new_refresh_token
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(os.path.abspath(config_path)), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, config_path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m unittest tests.test_kimi_watchdog.TestSaveRefreshToken -v`
Expected: 2 个测试 PASS

- [ ] **Step 5: 提交**

```bash
git add kimi_watchdog.py tests/test_kimi_watchdog.py
git commit -m "feat: 新增 save_refresh_token 原子写回配置文件"
```

---

### Task 3: `list_api_keys()` 与 `find_key_id()`

**Files:**
- Modify: `kimi_watchdog.py`
- Test: `tests/test_kimi_watchdog.py`

**Interfaces:**
- Consumes: `LIST_KEYS_URL`（Task 1）
- Produces: `_console_headers(access_token) -> dict`（Task 4 复用）、`list_api_keys(access_token) -> list[dict]`、`find_key_id(api_keys, api_key) -> (id, name) | None`（多匹配抛 RuntimeError）

- [ ] **Step 1: 写失败测试**

`tests/test_kimi_watchdog.py` 追加：

```python
class TestListApiKeys(unittest.TestCase):
    """list_api_keys：列出账号下 FEATURE_CODING 的 API Key"""

    def test_ok(self):
        """200：返回 apiKeys 列表；请求体与认证头正确"""
        resp = _FakeResponse({"apiKeys": [{"id": "id-1", "key": "sk-ki...dSFze"}]})
        with mock.patch.object(kw.urllib.request, "urlopen",
                               return_value=resp) as m:
            keys = kw.list_api_keys("at-1")
        self.assertEqual(keys, [{"id": "id-1", "key": "sk-ki...dSFze"}])
        req = m.call_args[0][0]
        self.assertEqual(req.headers["Authorization"], "Bearer at-1")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body, {"page_size": 100, "scope": ["FEATURE_CODING"]})


class TestFindKeyId(unittest.TestCase):
    """find_key_id：把完整 api_key 与列表中的掩码 key 匹配"""

    KEYS = [
        {"key": "sk-ki...dSFze", "name": "配额查询", "id": "id-1"},
        {"key": "sk-ki...4HPF8", "name": "DBX", "id": "id-2"},
    ]

    def test_single_match(self):
        """恰好 1 个匹配：返回 (id, name)"""
        self.assertEqual(kw.find_key_id(self.KEYS, "sk-kiAbCdEdSFze"),
                         ("id-1", "配额查询"))

    def test_no_match_returns_none(self):
        """0 个匹配：返回 None（Key 可能已被删除）"""
        self.assertIsNone(kw.find_key_id(self.KEYS, "sk-kiAbCdEzzzzz"))

    def test_multi_match_raises(self):
        """掩码后缀撞车匹配到多把：抛异常，拒绝删除"""
        keys = self.KEYS + [{"key": "sk-ki...dSFze", "name": "撞车", "id": "id-3"}]
        with self.assertRaises(RuntimeError):
            kw.find_key_id(keys, "sk-kiAbCdEdSFze")

    def test_malformed_masked_key_skipped(self):
        """不含 '...' 的异常条目直接跳过，不崩溃"""
        keys = [{"key": "sk-plain-no-mask", "name": "x", "id": "id-9"}]
        self.assertIsNone(kw.find_key_id(keys, "sk-plain-no-mask"))
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_kimi_watchdog.TestListApiKeys tests.test_kimi_watchdog.TestFindKeyId -v`
Expected: FAIL（`AttributeError: ... no attribute 'list_api_keys'` / `'find_key_id'`）

- [ ] **Step 3: 实现**

`kimi_watchdog.py`，`refresh_access_token` 之后新增：

```python
def _console_headers(access_token):
    """网页版控制台接口（apiv2 Connect RPC）的公共请求头。"""
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "Origin": "https://www.kimi.com",
        "Referer": "https://www.kimi.com/code/console",
    }


def list_api_keys(access_token):
    """列出账号下 scope 为 FEATURE_CODING 的全部 API Key。

    注意返回的 key 字段是掩码（如 "sk-ki...dSFze"），不是完整 Key。

    :param access_token: refresh_access_token 换来的 access_token
    :return: apiKeys 列表（dict 数组，含 id/name/key/status 等）
    :raises RuntimeError: HTTP 非 200
    :raises Exception: 网络错误、超时、JSON 解析失败等
    """
    body = json.dumps(
        {"page_size": 100, "scope": ["FEATURE_CODING"]}).encode("utf-8")
    req = urllib.request.Request(
        LIST_KEYS_URL, data=body, headers=_console_headers(access_token))
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        if resp.status != 200:
            raise RuntimeError(f"ListAPIKeys 返回状态码 {resp.status}")
        return json.loads(resp.read().decode("utf-8")).get("apiKeys", []) or []


def find_key_id(api_keys, api_key):
    """把 config 的完整 api_key 与列表中的掩码 key 匹配，找出要删的 Key。

    掩码形如 "sk-ki...dSFze"：按 "..." 拆前缀/后缀，api_key 同时满足
    startswith(前缀) 且 endswith(后缀) 即视为同一把。

    :param api_keys: list_api_keys 返回的列表
    :param api_key: config 中完整的 API Key
    :return: 恰好 1 个匹配时返回 (id, name)；0 个匹配返回 None
    :raises RuntimeError: 匹配到多把（掩码撞车），为安全起见拒绝删除
    """
    matches = []
    for item in api_keys:
        masked = item.get("key", "")
        if "..." not in masked:
            continue
        prefix, suffix = masked.split("...", 1)
        if api_key.startswith(prefix) and api_key.endswith(suffix):
            matches.append(item)
    if len(matches) > 1:
        names = "、".join(str(m.get("name")) for m in matches)
        raise RuntimeError(f"掩码匹配到多把 Key（{names}），为安全起见未删除")
    if not matches:
        return None
    return matches[0].get("id"), matches[0].get("name")
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m unittest tests.test_kimi_watchdog.TestListApiKeys tests.test_kimi_watchdog.TestFindKeyId -v`
Expected: 5 个测试 PASS

- [ ] **Step 5: 提交**

```bash
git add kimi_watchdog.py tests/test_kimi_watchdog.py
git commit -m "feat: 新增 list_api_keys 与掩码匹配 find_key_id"
```

---

### Task 4: `delete_api_key()`

**Files:**
- Modify: `kimi_watchdog.py`
- Test: `tests/test_kimi_watchdog.py`

**Interfaces:**
- Consumes: `DELETE_KEY_URL`（Task 1）、`_console_headers`（Task 3）
- Produces: `delete_api_key(access_token, key_id) -> None`（Task 5 调用）

- [ ] **Step 1: 写失败测试**

`tests/test_kimi_watchdog.py` 追加：

```python
class TestDeleteApiKey(unittest.TestCase):
    """delete_api_key：按 id 删除 API Key"""

    def test_ok(self):
        """200：不抛异常即成功；请求体为 {"id": ...}"""
        resp = _FakeResponse({})
        with mock.patch.object(kw.urllib.request, "urlopen",
                               return_value=resp) as m:
            kw.delete_api_key("at-1", "id-1")
        req = m.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body, {"id": "id-1"})
        self.assertEqual(req.headers["Authorization"], "Bearer at-1")

    def test_non_200_raises(self):
        """非 200 状态码抛异常"""
        with mock.patch.object(kw.urllib.request, "urlopen",
                               return_value=_FakeResponse({}, status=500)):
            with self.assertRaises(RuntimeError):
                kw.delete_api_key("at-1", "id-1")
```

注意：`_FakeResponse` 模拟的是 urlopen 不抛 HTTPError 直接返回非 200 的情形（urllib 真实行为是抛 HTTPError，但实现里两种路径都有兜底，测试沿用现有 `TestFetchUsage.test_non_200_raises` 的同款写法）。

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_kimi_watchdog.TestDeleteApiKey -v`
Expected: FAIL（`AttributeError: ... no attribute 'delete_api_key'`）

- [ ] **Step 3: 实现**

`kimi_watchdog.py`，`find_key_id` 之后新增：

```python
def delete_api_key(access_token, key_id):
    """删除指定 id 的 API Key。HTTP 200 即成功，响应体不解析。

    :param access_token: refresh_access_token 换来的 access_token
    :param key_id: find_key_id 找出的 Key id
    :raises RuntimeError: HTTP 非 200
    :raises Exception: 网络错误、超时等
    """
    body = json.dumps({"id": key_id}).encode("utf-8")
    req = urllib.request.Request(
        DELETE_KEY_URL, data=body, headers=_console_headers(access_token))
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        if resp.status != 200:
            raise RuntimeError(f"DeleteAPIKey 返回状态码 {resp.status}")
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m unittest tests.test_kimi_watchdog.TestDeleteApiKey -v`
Expected: 2 个测试 PASS

- [ ] **Step 5: 提交**

```bash
git add kimi_watchdog.py tests/test_kimi_watchdog.py
git commit -m "feat: 新增 delete_api_key 按 id 删除 API Key"
```

---

### Task 5: 主循环额度触发分支集成（删除链路 + 退出码 4）

**Files:**
- Modify: `kimi_watchdog.py`（`main` 的额度触发分支、新增 `_attempt_key_deletion`）
- Test: `tests/test_kimi_watchdog.py`

**Interfaces:**
- Consumes: Task 1-4 的全部函数、`EXIT_DELETE_FAILED`
- Produces: `_attempt_key_deletion(cfg, config_path) -> (ok, detail)`（Task 6 的 `--test-delete` 复用其中三步：refresh/list/find）

- [ ] **Step 1: 写失败测试**

`tests/test_kimi_watchdog.py` 追加（`LOOP_CFG` 定义之后的位置均可，这里放文件末尾）：

```python
class TestMainLoopKeyDeletion(unittest.TestCase):
    """额度触发时的 Key 删除链路（refresh/save/list/delete 全部 mock）"""

    CFG_RT = dict(LOOP_CFG, refresh_token="rt-1")
    # find_key_id 用真实实现：掩码 "sk-...test" 前后缀匹配 LOOP_CFG 的 "sk-test"
    KEY_ENTRY = {"key": "sk-...test", "name": "监控", "id": "id-1"}

    def _run(self, cfg, fetch, refresh=None, save=None, keys=None, delete=None):
        with mock.patch.object(kw, "load_config", return_value=dict(cfg)), \
                mock.patch.object(kw.time, "sleep"), \
                mock.patch.object(kw, "send_serverchan") as m_send, \
                mock.patch.object(kw, "fetch_usage", fetch), \
                mock.patch.object(kw, "refresh_access_token",
                                  refresh or mock.Mock(return_value=("at-1", "rt-2"))), \
                mock.patch.object(kw, "save_refresh_token",
                                  save or mock.Mock()) as m_save, \
                mock.patch.object(kw, "list_api_keys",
                                  keys if keys is not None
                                  else mock.Mock(return_value=[self.KEY_ENTRY])), \
                mock.patch.object(kw, "delete_api_key",
                                  delete or mock.Mock()) as m_del:
            code = kw.main(["90", "2099-12-31 23:59"])
        return code, m_send, m_save, m_del

    def test_delete_success(self):
        """删除成功：exit 1，save 写回新 refresh_token，通知含“已删除”"""
        code, m_send, m_save, m_del = self._run(
            self.CFG_RT, mock.Mock(return_value=_make_usage_data("5")))
        self.assertEqual(code, kw.EXIT_QUOTA)
        m_save.assert_called_once()
        self.assertEqual(m_save.call_args[0][1], "rt-2")
        m_del.assert_called_once_with("at-1", "id-1")
        self.assertIn("已删除", m_send.call_args[0][2])

    def test_save_failure_still_deletes(self):
        """写回失败不阻断删除：exit 1，通知附警告"""
        code, m_send, _, m_del = self._run(
            self.CFG_RT, mock.Mock(return_value=_make_usage_data("5")),
            save=mock.Mock(side_effect=OSError("disk full")))
        self.assertEqual(code, kw.EXIT_QUOTA)
        m_del.assert_called_once()
        self.assertIn("写回", m_send.call_args[0][2])

    def test_refresh_401_aborts_delete(self):
        """刷新 401：exit 4，不删不写回，通知提示重新抓取"""
        err = RuntimeError("刷新接口返回状态码 401")
        err.status = 401
        code, m_send, m_save, m_del = self._run(
            self.CFG_RT, mock.Mock(return_value=_make_usage_data("5")),
            refresh=mock.Mock(side_effect=err))
        self.assertEqual(code, kw.EXIT_DELETE_FAILED)
        m_save.assert_not_called()
        m_del.assert_not_called()
        self.assertIn("重新抓取", m_send.call_args[0][2])

    def test_no_refresh_token_legacy_behavior(self):
        """未配置 refresh_token：不删，exit 1（行为与现状一致）"""
        code, _, m_save, m_del = self._run(
            LOOP_CFG, mock.Mock(return_value=_make_usage_data("5")))
        self.assertEqual(code, kw.EXIT_QUOTA)
        m_save.assert_not_called()
        m_del.assert_not_called()

    def test_no_match_is_success(self):
        """0 匹配（Key 可能已删）：视为无需处理，exit 1，不调用删除"""
        code, m_send, _, m_del = self._run(
            self.CFG_RT, mock.Mock(return_value=_make_usage_data("5")),
            keys=mock.Mock(return_value=[]))
        self.assertEqual(code, kw.EXIT_QUOTA)
        m_del.assert_not_called()
        self.assertIn("无需", m_send.call_args[0][2])

    def test_multi_match_aborts(self):
        """多匹配：exit 4，不删除"""
        dup = [self.KEY_ENTRY, dict(self.KEY_ENTRY, id="id-2")]
        code, _, _, m_del = self._run(
            self.CFG_RT, mock.Mock(return_value=_make_usage_data("5")),
            keys=mock.Mock(return_value=dup))
        self.assertEqual(code, kw.EXIT_DELETE_FAILED)
        m_del.assert_not_called()
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_kimi_watchdog.TestMainLoopKeyDeletion -v`
Expected: FAIL（`AttributeError: ... no attribute 'refresh_access_token'` 已被 mock，实际失败原因是退出码不符 / `_attempt_key_deletion` 不存在）

- [ ] **Step 3: 实现**

`kimi_watchdog.py`，`delete_api_key` 之后新增：

```python
def _attempt_key_deletion(cfg, config_path):
    """额度触发后执行删除链路：刷新 → 写回 → 列表 → 匹配 → 删除。

    0 匹配（Key 可能已被手动删除）视为成功（无需处理）；写回失败不阻断删除，
    仅在结果描述中附警告。

    :param cfg: 配置字典（需含 refresh_token、api_key）
    :param config_path: 配置文件路径（写回 refresh_token 用）
    :return: (是否成功, 结果描述)；False 对应退出码 EXIT_DELETE_FAILED
    """
    try:
        access_token, new_refresh = refresh_access_token(cfg["refresh_token"])
    except Exception as e:
        if getattr(e, "status", None) == 401:
            return False, ("refresh_token 已失效，请重新登录 kimi.com 后"
                           "从 localStorage 重新抓取")
        return False, f"刷新 access_token 失败：{e}"
    # 刷新成功：立即把轮换出的新 refresh_token 写回（90 天有效期滚动续期）
    warning = ""
    try:
        save_refresh_token(config_path, new_refresh)
    except Exception as e:
        warning = f"（警告：新 refresh_token 写回 {config_path} 失败：{e}）"
    try:
        found = find_key_id(list_api_keys(access_token), cfg["api_key"])
        if found is None:
            return True, "未找到匹配的 Key，可能已被删除，无需处理" + warning
        delete_api_key(access_token, found[0])
        return True, f"已删除 Key：name={found[1]} id={found[0]}" + warning
    except Exception as e:
        return False, f"{e}" + warning
```

`main` 中的额度触发分支，把这段：

```python
            # ---- 条件二：用量达到阈值 ----
            if pct >= args.percent:
                reason = f"本周用量已达 {pct:.1f}%（阈值 {args.percent:g}%）"
                title, body = build_message(reason, info)
                _send_and_print(cfg, title, body, reason)
                return EXIT_QUOTA
```

改为：

```python
            # ---- 条件二：用量达到阈值 ----
            if pct >= args.percent:
                reason = f"本周用量已达 {pct:.1f}%（阈值 {args.percent:g}%）"
                title, body = build_message(reason, info)
                exit_code = EXIT_QUOTA
                # 配置了 refresh_token 才启用自动删除；未配置时行为与原来一致
                if cfg.get("refresh_token"):
                    ok, detail = _attempt_key_deletion(cfg, args.config)
                    body += f"\n\nKey 删除：{detail}"
                    if not ok:
                        exit_code = EXIT_DELETE_FAILED
                _send_and_print(cfg, title, body, reason)
                return exit_code
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m unittest tests.test_kimi_watchdog -v`
Expected: 全部 PASS（含既有用例回归）

- [ ] **Step 5: 提交**

```bash
git add kimi_watchdog.py tests/test_kimi_watchdog.py
git commit -m "feat: 额度触发时自动删除对应 API Key（新增退出码 4）"
```

---

### Task 6: `--test-delete` 干跑模式

**Files:**
- Modify: `kimi_watchdog.py`（argparse 与 main 的模式分支）
- Test: `tests/test_kimi_watchdog.py`

**Interfaces:**
- Consumes: `refresh_access_token`、`list_api_keys`、`find_key_id`（Task 1/3）
- Produces: CLI 新参数 `--test-delete`（干跑：刷新+匹配，**不删除、不写回**；成功 exit 0，失败 exit 3）

- [ ] **Step 1: 写失败测试**

`tests/test_kimi_watchdog.py` 追加：

```python
class TestTestDeleteMode(unittest.TestCase):
    """--test-delete 干跑模式：验证链路但不真删、不写回"""

    CFG_RT = dict(LOOP_CFG, refresh_token="rt-1")
    KEY_ENTRY = {"key": "sk-...test", "name": "监控", "id": "id-1"}

    def _run(self, cfg, refresh=None, keys=None):
        with mock.patch.object(kw, "load_config", return_value=dict(cfg)), \
                mock.patch.object(kw, "refresh_access_token",
                                  refresh or mock.Mock(return_value=("at-1", "rt-2"))), \
                mock.patch.object(kw, "list_api_keys",
                                  keys if keys is not None
                                  else mock.Mock(return_value=[self.KEY_ENTRY])), \
                mock.patch.object(kw, "delete_api_key") as m_del, \
                mock.patch.object(kw, "save_refresh_token") as m_save:
            code = kw.main(["--test-delete"])
        return code, m_del, m_save

    def test_dry_run_success(self):
        """匹配成功：exit 0，不调用删除、不写回"""
        code, m_del, m_save = self._run(self.CFG_RT)
        self.assertEqual(code, kw.EXIT_OK)
        m_del.assert_not_called()
        m_save.assert_not_called()

    def test_dry_run_no_match_returns_error(self):
        """0 匹配：exit 3（说明配置/账号状态有问题，值得告警）"""
        code, _, _ = self._run(self.CFG_RT, keys=mock.Mock(return_value=[]))
        self.assertEqual(code, kw.EXIT_ERROR)

    def test_dry_run_missing_refresh_token(self):
        """未配置 refresh_token：exit 3"""
        with mock.patch.object(kw, "load_config", return_value=dict(LOOP_CFG)):
            code = kw.main(["--test-delete"])
        self.assertEqual(code, kw.EXIT_ERROR)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_kimi_watchdog.TestTestDeleteMode -v`
Expected: FAIL（argparse 报无法识别的参数 `--test-delete`）

- [ ] **Step 3: 实现**

`kimi_watchdog.py` 的 `main` 中，`--test-notify` 参数定义之后加：

```python
    parser.add_argument("--test-delete", action="store_true",
                        help="删除链路干跑：刷新+匹配并打印将删除的 Key，但不真正删除")
```

`--test-notify` 模式分支之后加：

```python
    # --test-delete 模式：删除链路干跑（刷新+匹配，不删除、不写回）
    if args.test_delete:
        if not cfg.get("refresh_token"):
            print("错误：未配置 refresh_token，请检查 config.json")
            return EXIT_ERROR
        print("正在执行删除链路干跑（不会真正删除）...")
        try:
            access_token, _ = refresh_access_token(cfg["refresh_token"])
            found = find_key_id(list_api_keys(access_token), cfg["api_key"])
        except Exception as e:
            print(f"干跑失败：{e}")
            return EXIT_ERROR
        if found is None:
            print("未找到匹配的 Key（可能已被删除），请检查 api_key 配置")
            return EXIT_ERROR
        print(f"干跑成功：额度触发时将删除 name={found[1]} id={found[0]}")
        return EXIT_OK
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m unittest tests.test_kimi_watchdog -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add kimi_watchdog.py tests/test_kimi_watchdog.py
git commit -m "feat: 新增 --test-delete 删除链路干跑自检"
```

---

### Task 7: 启动自检（refresh_token 有效期本地预检）

**Files:**
- Modify: `kimi_watchdog.py`（import、main 的配置加载后）
- Test: `tests/test_kimi_watchdog.py`

**Interfaces:**
- Consumes: 无
- Produces: `check_refresh_token_expiry(refresh_token) -> float | None`；main 启动时的警告输出

- [ ] **Step 1: 写失败测试**

`tests/test_kimi_watchdog.py` 顶部 import 区加 `import base64`、`import time`，文件末尾追加：

```python
class TestCheckRefreshTokenExpiry(unittest.TestCase):
    """check_refresh_token_expiry：本地解码 JWT exp（不发请求）"""

    def _jwt(self, exp):
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
        return f"aaa.{payload}.bbb"

    def test_days_remaining(self):
        """30 天后过期：返回值约等于 30"""
        days = kw.check_refresh_token_expiry(self._jwt(time.time() + 30 * 86400))
        self.assertAlmostEqual(days, 30.0, places=1)

    def test_expired_is_negative(self):
        """已过期：返回负数"""
        self.assertLess(kw.check_refresh_token_expiry(
            self._jwt(time.time() - 86400)), 0)

    def test_garbage_returns_none(self):
        """无法解析：返回 None"""
        self.assertIsNone(kw.check_refresh_token_expiry("not-a-jwt"))


class TestStartupExpiryWarning(unittest.TestCase):
    """启动自检：过期/临期 refresh_token 只警告，不阻断监控"""

    def _expired_cfg(self):
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": time.time() - 86400}).encode()).rstrip(b"=").decode()
        return dict(LOOP_CFG, refresh_token=f"aaa.{payload}.bbb")

    def test_expired_token_warns_but_monitors(self):
        """过期的 refresh_token：打印警告，监控照常触发退出"""
        with mock.patch.object(kw, "load_config",
                               return_value=self._expired_cfg()), \
                mock.patch.object(kw.time, "sleep"), \
                mock.patch.object(kw, "send_serverchan"), \
                mock.patch.object(kw, "refresh_access_token",
                                  return_value=("at-1", "rt-2")), \
                mock.patch.object(kw, "save_refresh_token"), \
                mock.patch.object(kw, "list_api_keys", return_value=[]), \
                mock.patch.object(kw, "fetch_usage",
                                  return_value=_make_usage_data("5")), \
                io.StringIO() as buf, \
                contextlib.redirect_stdout(buf):
            code = kw.main(["90", "2099-12-31 23:59"])
        self.assertEqual(code, kw.EXIT_QUOTA)
        self.assertIn("警告", buf.getvalue())
```

并在 import 区加 `import contextlib`、`import io`。

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_kimi_watchdog.TestCheckRefreshTokenExpiry -v`
Expected: FAIL（`AttributeError: ... no attribute 'check_refresh_token_expiry'`）

- [ ] **Step 3: 实现**

`kimi_watchdog.py` import 区加 `import base64`。`save_refresh_token` 之后新增：

```python
def check_refresh_token_expiry(refresh_token):
    """本地解码 refresh_token 的 JWT exp（不验签、不发请求），用于启动自检。

    :param refresh_token: JWT 字符串
    :return: 剩余有效天数（float，负数=已过期）；无法解析返回 None
    """
    try:
        payload = refresh_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return (claims["exp"] - time.time()) / 86400
    except Exception:
        return None
```

`main` 中 `cfg = load_config(args.config)` 之后加：

```python
    # ---- refresh_token 有效期预检：只警告，不阻断监控 ----
    if cfg.get("refresh_token"):
        days = check_refresh_token_expiry(cfg["refresh_token"])
        if days is None:
            print("[警告] refresh_token 无法解析（不是合法 JWT），删除功能将不可用")
        elif days < 0:
            print("[警告] refresh_token 已过期，删除功能将不可用，请重新抓取")
        elif days < 7:
            print(f"[提醒] refresh_token 剩余有效期约 {days:.1f} 天，建议尽快重新抓取")
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m unittest tests.test_kimi_watchdog -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add kimi_watchdog.py tests/test_kimi_watchdog.py
git commit -m "feat: 启动时本地预检 refresh_token 有效期并告警"
```

---

### Task 8: 文档、示例配置与收尾

**Files:**
- Modify: `kimi_watchdog.py`（模块 docstring）、`README.md`、`config.example.json`
- Delete: `verify_refresh.py`
- Modify（不提交）: `config.json`（写入最新 refresh_token）

**Interfaces:**
- Consumes: 全部前序任务
- Produces: 无新代码接口

- [ ] **Step 1: 更新模块 docstring**

`kimi_watchdog.py` 顶部 docstring 的功能段加一行「额度阈值触发时，若配置了 refresh_token，自动删除 config 中 api_key 对应的 API Key 后通知退出」；用法行改为：

```
    python kimi_watchdog.py <percent> <time> [--test-notify] [--test-delete]
```

退出码段加：`4 = 额度阈值触发，但删除链路失败`。

- [ ] **Step 2: 更新 config.example.json**

改为：

```json
{
  "api_key": "sk-你的KimiCode的APIKey",
  "poll_interval_sec": 600,
  "serverchan_sendkey": "SCT你的Server酱SendKey",
  "refresh_token": ""
}
```

- [ ] **Step 3: 更新 README**

- 参数表加一行：`` `--test-delete` `` | 删除链路干跑：刷新+匹配并打印将删除的 Key，但不真正删除 |
- 退出码表加一行：`| 4 | 额度阈值触发，但删除链路失败 |`
- 配置项表加一行：`` `refresh_token` `` | 空 | 网页版 localStorage 的 refresh_token；配置后额度触发时自动删除 api_key 对应的 Key |
- 新增小节「自动删除 API Key」，内容要点：

```markdown
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
  （打印将删除的 Key，但不真正删除）
```

- [ ] **Step 4: 删除一次性验证脚本并运行全量测试**

```bash
rm verify_refresh.py
python -m unittest tests.test_kimi_watchdog -v
```

Expected: 全部 PASS

- [ ] **Step 5: 把最新 refresh_token 写入本机 config.json（不提交）**

用实现好的 `save_refresh_token` 自我验证（dogfooding）。**token 值不写进任何提交**：
执行者从本次会话的 `verify_refresh.py` 验证输出中取「NEW refresh_token」最新值；
若上下文已不可用，向用户索取后执行：

```bash
python -c "import os, kimi_watchdog as kw; kw.save_refresh_token('config.json', os.environ['KIMI_RT'])"
```

（先 `export KIMI_RT=<最新 refresh_token>`，避免值出现在命令历史和提交里。）
随后让用户运行 `python kimi_watchdog.py --test-delete` 做真实干跑验收（会真实刷新一次 token 并再次写回 config，属预期行为）。

- [ ] **Step 6: 提交**

```bash
git add kimi_watchdog.py README.md config.example.json
git rm --cached verify_refresh.py 2>/dev/null; git add -A verify_refresh.py
git commit -m "docs: 补充自动删除 Key 文档与示例配置；移除一次性验证脚本"
```

---

## Self-Review 记录

- Spec 覆盖：配置(§4.1)→Task 8/7；四个新函数(§4.2)→Task 1-4；主流程与错误矩阵(§4.3)→Task 5；
  退出码(§4.4)→Task 5/8；--test-delete(§4.5)→Task 6；测试计划(§5)→各 Task Step 1；文档收尾(§6)→Task 8。无遗漏。
- 类型一致性：`refresh_access_token` 返回二元组，Task 5/6 均以 `access_token, new_refresh = ...` / `access_token, _ = ...` 解包；`find_key_id` 返回 `(id, name)`，Task 5 用 `found[0]`/`found[1]`，测试断言 `("id-1", "配额查询")`，一致。
- 测试中的掩码 `"sk-...test"` 与 `LOOP_CFG` 的 `api_key="sk-test"`：前缀 `"sk-"` + 后缀 `"test"` 双向匹配成立。
