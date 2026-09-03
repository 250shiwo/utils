# kimi-watchdog 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 单文件零依赖 Python 脚本，监控 Kimi Code 周额度，当用量达到阈值或到达指定时刻时通过 Server酱 + QQ邮箱 通知后退出。

**Architecture:** 单文件 `kimi_watchdog.py` 承载全部逻辑（配置加载 → 轮询 API → 条件判断 → 双渠道通知 → 退出），函数级模块划分，`config.json` 提供配置。测试用标准库 `unittest` + `unittest.mock`。

**Tech Stack:** Python 3.8+ 标准库（urllib / smtplib / email / json / argparse / datetime / time），无任何第三方依赖。

## Global Constraints

- Python 3.8+，仅标准库，禁止引入任何第三方依赖。
- 全部逻辑在单文件 `kimi_watchdog.py` 中，带详细中文注释（模块 docstring + 函数 docstring + 关键逻辑行内注释）。
- 数据接口：`GET https://api.kimi.com/coding/v1/usages`，Header `Authorization: Bearer <key>`，超时 10 秒。
- 退出码：0=正常/测试通知成功/Ctrl+C；1=额度触发；2=时间触发；3=监控异常（API 连续 5 次失败）。
- `config.json` 不入 git，提供 `config.example.json` 模板；`KIMI_API_KEY` 环境变量优先于配置文件。
- 测试命令统一为 `python -m unittest tests.test_kimi_watchdog -v`（在工作区根目录执行）。
- **提交信息为中文，且由于 Windows 控制台编码问题，`git commit -m "中文"` 会乱码**。提交必须用文件方式：先用 write_to_file 工具写 `.gcm.txt`（UTF-8），再执行 `git commit -F .gcm.txt`，提交后删除 `.gcm.txt`。本计划每个提交步骤均已按此格式给出。

---

### Task 1: 项目骨架与配置加载

**Files:**
- Create: `kimi_watchdog.py`
- Create: `config.example.json`
- Create: `.gitignore`
- Create: `tests/__init__.py`（空文件）
- Create: `tests/test_kimi_watchdog.py`

**Interfaces:**
- Produces: `load_config(config_path=CONFIG_FILE) -> dict`（键：`api_key`、`poll_interval_sec`、`serverchan_sendkey`、`email`）；常量 `API_URL`、`CONFIG_FILE`、`HTTP_TIMEOUT`、`MAX_CONSECUTIVE_FAILURES`、`EXIT_OK/EXIT_QUOTA/EXIT_TIME/EXIT_ERROR`

- [ ] **Step 1: 写失败测试**

`tests/test_kimi_watchdog.py` 完整内容：

```python
# -*- coding: utf-8 -*-
"""kimi_watchdog 单元测试（标准库 unittest，无需安装任何依赖）"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

# 将项目根目录加入 sys.path，以便直接 import 被测脚本
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kimi_watchdog as kw  # noqa: E402


class TestLoadConfig(unittest.TestCase):
    """load_config：读取配置文件 + 环境变量优先"""

    def _write_temp_config(self, content):
        """把配置字典写入临时文件，返回文件路径"""
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(content, f)
        self.addCleanup(os.remove, path)
        return path

    def test_missing_file_returns_defaults(self):
        """配置文件不存在时返回默认值"""
        cfg = kw.load_config("no_such_file.json")
        self.assertEqual(cfg["api_key"], "")
        self.assertEqual(cfg["poll_interval_sec"], 600)
        self.assertEqual(cfg["serverchan_sendkey"], "")
        self.assertIsNone(cfg["email"])

    def test_load_from_file(self):
        """正常读取配置文件"""
        path = self._write_temp_config({
            "api_key": "sk-file",
            "poll_interval_sec": 300,
            "serverchan_sendkey": "SCT123",
            "email": {"smtp_host": "smtp.qq.com"},
        })
        cfg = kw.load_config(path)
        self.assertEqual(cfg["api_key"], "sk-file")
        self.assertEqual(cfg["poll_interval_sec"], 300)
        self.assertEqual(cfg["serverchan_sendkey"], "SCT123")
        self.assertEqual(cfg["email"]["smtp_host"], "smtp.qq.com")

    def test_env_var_overrides_file(self):
        """环境变量 KIMI_API_KEY 优先于配置文件中的 api_key"""
        path = self._write_temp_config({"api_key": "sk-file"})
        with mock.patch.dict(os.environ, {"KIMI_API_KEY": "sk-env"}):
            cfg = kw.load_config(path)
        self.assertEqual(cfg["api_key"], "sk-env")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest tests.test_kimi_watchdog -v`
Expected: FAIL/ERROR（`ModuleNotFoundError: No module named 'kimi_watchdog'`）

- [ ] **Step 3: 写最小实现**

`kimi_watchdog.py` 完整内容：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kimi-watchdog：Kimi Code API 周额度监控脚本

功能：
    启动时传入「用量百分比阈值」和「目标时刻」两个参数，脚本常驻轮询
    Kimi Code 用量接口；任一条件满足时，通过 Server酱（微信推送）和
    QQ邮箱（SMTP）发送提醒通知，然后退出。

用法：
    python kimi_watchdog.py <percent> <time> [--test-notify]
    例：python kimi_watchdog.py 80 18:00

退出码：
    0 = 正常退出（含 --test-notify 成功、Ctrl+C 主动停止）
    1 = 额度阈值触发
    2 = 指定时刻触发
    3 = 监控异常（API 连续 5 次请求失败）
"""

import argparse
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

# ---------------- 常量定义 ----------------
API_URL = "https://api.kimi.com/coding/v1/usages"  # Kimi Code 用量查询接口（非官方，社区逆向）
CONFIG_FILE = "config.json"                        # 默认配置文件路径
SERVERCHAN_URL = "https://sctapi.ftqq.com/{}.send"  # Server酱推送接口，{} 处填 sendkey
HTTP_TIMEOUT = 10                                   # HTTP 请求超时（秒）
MAX_CONSECUTIVE_FAILURES = 5                        # API 连续失败多少次后判定监控异常

# 退出码约定（见模块 docstring）
EXIT_OK = 0
EXIT_QUOTA = 1
EXIT_TIME = 2
EXIT_ERROR = 3


# ==================== 配置加载 ====================
def load_config(config_path=CONFIG_FILE):
    """加载配置文件并与默认值合并。

    优先级：环境变量 KIMI_API_KEY > 配置文件 > 默认值。
    :param config_path: 配置文件路径，默认为当前目录下 config.json
    :return: 配置字典，键包括：
        api_key           Kimi Code API Key（字符串，可能为空）
        poll_interval_sec 轮询间隔秒数（默认 600）
        serverchan_sendkey Server酱 SendKey（空字符串表示不启用）
        email             邮件配置字典（None 表示不启用）
    """
    # 默认配置
    cfg = {
        "api_key": "",
        "poll_interval_sec": 600,
        "serverchan_sendkey": "",
        "email": None,
    }
    # 配置文件存在则覆盖默认值
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    # 环境变量优先级最高
    if os.environ.get("KIMI_API_KEY"):
        cfg["api_key"] = os.environ["KIMI_API_KEY"]
    return cfg
```

`config.example.json` 完整内容：

```json
{
  "api_key": "sk-你的KimiCode的APIKey",
  "poll_interval_sec": 600,
  "serverchan_sendkey": "SCT你的Server酱SendKey，留空则不启用",
  "email": {
    "smtp_host": "smtp.qq.com",
    "smtp_port": 465,
    "username": "你的QQ号@qq.com",
    "auth_code": "QQ邮箱SMTP授权码（不是QQ密码）",
    "to": "收件人邮箱@qq.com"
  }
}
```

`.gitignore` 完整内容：

```
# 本地隐私配置（含 API Key / 授权码），不入库
config.json

# Python
__pycache__/
*.pyc

# 临时文件
.gcm.txt
```

`tests/__init__.py`：空文件。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m unittest tests.test_kimi_watchdog -v`
Expected: 3 tests PASS

- [ ] **Step 5: 提交**

写 `.gcm.txt` 内容为：

```
feat: 初始化项目骨架与配置加载
```

Run: `git add kimi_watchdog.py config.example.json .gitignore tests/ && git commit -F .gcm.txt && del .gcm.txt`
Expected: 提交成功

---

### Task 2: 时间参数解析

**Files:**
- Modify: `kimi_watchdog.py`（文件末尾追加函数）
- Modify: `tests/test_kimi_watchdog.py`（文件末尾、`if __name__` 之前追加测试类）

**Interfaces:**
- Produces: `parse_deadline(time_str, now=None) -> datetime`。支持 `"HH:MM"`（今天，已过则顺延明天）与 `"YYYY-MM-DD HH:MM"` 两种格式；格式非法抛 `ValueError`。`now` 参数仅用于测试注入。

- [ ] **Step 1: 写失败测试**

在 `tests/test_kimi_watchdog.py` 的 `if __name__ == "__main__":` 之前追加：

```python
class TestParseDeadline(unittest.TestCase):
    """parse_deadline：时间参数解析"""

    # 固定"当前时间"，保证测试可重复
    NOW = datetime(2026, 9, 3, 10, 0, 0)

    def test_hhmm_today(self):
        """今天还没到的时刻 -> 今天"""
        target = kw.parse_deadline("18:00", now=self.NOW)
        self.assertEqual(target, datetime(2026, 9, 3, 18, 0))

    def test_hhmm_rollover_tomorrow(self):
        """今天已过的时刻 -> 顺延到明天同一时刻"""
        target = kw.parse_deadline("08:00", now=self.NOW)
        self.assertEqual(target, datetime(2026, 9, 4, 8, 0))

    def test_hhmm_exactly_now_rolls_over(self):
        """恰好等于当前时刻 -> 视为已过，顺延明天"""
        target = kw.parse_deadline("10:00", now=self.NOW)
        self.assertEqual(target, datetime(2026, 9, 4, 10, 0))

    def test_full_datetime(self):
        """完整日期时间格式"""
        target = kw.parse_deadline("2026-09-03 18:00", now=self.NOW)
        self.assertEqual(target, datetime(2026, 9, 3, 18, 0))

    def test_invalid_format_raises(self):
        """非法格式抛 ValueError，错误信息包含原始输入"""
        with self.assertRaises(ValueError):
            kw.parse_deadline("明天八点", now=self.NOW)
```

同时在该测试文件顶部的 import 区域追加（若尚无）：

```python
from datetime import datetime
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest tests.test_kimi_watchdog -v`
Expected: 新增 5 个测试 ERROR/FAIL（`AttributeError: module 'kimi_watchdog' has no attribute 'parse_deadline'`），原 3 个仍 PASS

- [ ] **Step 3: 写最小实现**

在 `kimi_watchdog.py` 文件末尾追加：

```python
# ==================== 时间参数解析 ====================
def parse_deadline(time_str, now=None):
    """解析时间参数为目标时刻（datetime 对象）。

    支持两种格式：
        "HH:MM"          -> 今天的该时刻；若启动时已过（<= now）则顺延到明天
        "YYYY-MM-DD HH:MM" -> 完整日期时间，按字面解析
    :param time_str: 时间参数字符串
    :param now: 当前时间（仅用于测试注入，默认 datetime.now()）
    :return: 目标时刻 datetime 对象
    :raises ValueError: 两种格式都无法解析时
    """
    if now is None:
        now = datetime.now()
    time_str = time_str.strip()

    # 先尝试 "HH:MM" 格式
    try:
        t = datetime.strptime(time_str, "%H:%M")
        target = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        # 今天该时刻已过（或恰好等于现在）则顺延到明天
        if target <= now:
            target += timedelta(days=1)
        return target
    except ValueError:
        pass

    # 再尝试 "YYYY-MM-DD HH:MM" 完整格式
    try:
        return datetime.strptime(time_str, "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError(
            f"无法解析时间参数: {time_str!r}，支持 'HH:MM' 或 'YYYY-MM-DD HH:MM' 格式"
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m unittest tests.test_kimi_watchdog -v`
Expected: 8 tests PASS

- [ ] **Step 5: 提交**

`.gcm.txt` 内容：

```
feat: 实现时间参数解析，支持今天/明天顺延与完整日期格式
```

Run: `git add kimi_watchdog.py tests/test_kimi_watchdog.py && git commit -F .gcm.txt && del .gcm.txt`
Expected: 提交成功

---

### Task 3: 用量获取与解析

**Files:**
- Modify: `kimi_watchdog.py`
- Modify: `tests/test_kimi_watchdog.py`

**Interfaces:**
- Produces:
  - `fetch_usage(api_key) -> dict`：请求 API 并返回解析后的 JSON 字典；任何失败（网络/超时/非 200/JSON 错误）抛异常
  - `extract_usage_info(data) -> dict`：把 API 原始 JSON 规整为 `{"limit", "remaining", "reset_time", "window_remaining", "membership"}`（值可能为 None）
  - `compute_used_percent(usage) -> float`：入参为含 `limit`/`remaining` 键的字典（值可为字符串），返回周用量百分比；`limit <= 0` 时返回 0.0

- [ ] **Step 1: 写失败测试**

在 `tests/test_kimi_watchdog.py` 的 `if __name__ == "__main__":` 之前追加：

```python
# API 返回样例（来自社区逆向工程文档），各任务复用
SAMPLE_API_DATA = {
    "usage": {
        "limit": "100",        # 本周总配额
        "remaining": "74",     # 本周剩余配额
        "resetTime": "2026-02-11T17:32:50.757941Z",
    },
    "limits": [
        {
            "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
            "detail": {"limit": "100", "remaining": "85",
                       "resetTime": "2026-02-07T12:32:50.757941Z"},
        }
    ],
    "user": {"membership": {"level": "LEVEL_INTERMEDIATE"}},
}


class _FakeResponse:
    """模拟 urllib 的响应对象"""

    def __init__(self, payload, status=200):
        self._payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestFetchUsage(unittest.TestCase):
    """fetch_usage：API 请求"""

    def test_ok(self):
        """正常返回：携带 Bearer 头，解析 JSON"""
        with mock.patch.object(kw.urllib.request, "urlopen",
                               return_value=_FakeResponse(SAMPLE_API_DATA)) as m:
            data = kw.fetch_usage("sk-test")
        self.assertEqual(data["usage"]["remaining"], "74")
        # 校验请求头中包含 Bearer 认证
        req = m.call_args[0][0]
        self.assertEqual(req.headers["Authorization"], "Bearer sk-test")

    def test_non_200_raises(self):
        """非 200 状态码抛异常"""
        with mock.patch.object(kw.urllib.request, "urlopen",
                               return_value=_FakeResponse({}, status=500)):
            with self.assertRaises(RuntimeError):
                kw.fetch_usage("sk-test")

    def test_bad_json_raises(self):
        """返回非 JSON 抛异常"""
        bad = _FakeResponse(SAMPLE_API_DATA)
        bad._payload = b"not json"
        with mock.patch.object(kw.urllib.request, "urlopen", return_value=bad):
            with self.assertRaises(Exception):
                kw.fetch_usage("sk-test")


class TestExtractUsageInfo(unittest.TestCase):
    """extract_usage_info：原始 JSON 规整"""

    def test_full_data(self):
        info = kw.extract_usage_info(SAMPLE_API_DATA)
        self.assertEqual(info["limit"], "100")
        self.assertEqual(info["remaining"], "74")
        self.assertEqual(info["reset_time"], "2026-02-11T17:32:50.757941Z")
        self.assertEqual(info["window_remaining"], "85")
        self.assertEqual(info["membership"], "LEVEL_INTERMEDIATE")

    def test_empty_data(self):
        """字段缺失时返回 None 而不是崩溃"""
        info = kw.extract_usage_info({})
        self.assertIsNone(info["limit"])
        self.assertIsNone(info["remaining"])
        self.assertIsNone(info["reset_time"])
        self.assertIsNone(info["window_remaining"])
        self.assertEqual(info["membership"], "")


class TestComputeUsedPercent(unittest.TestCase):
    """compute_used_percent：周用量百分比"""

    def test_normal(self):
        # (100 - 74) / 100 = 26%
        self.assertAlmostEqual(
            kw.compute_used_percent({"limit": "100", "remaining": "74"}), 26.0)

    def test_zero_limit(self):
        """limit 为 0 时返回 0.0，避免除零"""
        self.assertEqual(kw.compute_used_percent({"limit": "0", "remaining": "0"}), 0.0)

    def test_all_used(self):
        self.assertAlmostEqual(
            kw.compute_used_percent({"limit": "50", "remaining": "0"}), 100.0)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest tests.test_kimi_watchdog -v`
Expected: 新增 8 个测试 FAIL/ERROR（无 `fetch_usage` 等属性），原 8 个仍 PASS

- [ ] **Step 3: 写最小实现**

在 `kimi_watchdog.py` 文件末尾追加：

```python
# ==================== 用量获取与解析 ====================
def fetch_usage(api_key):
    """调用 Kimi Code 用量接口，返回解析后的 JSON 字典。

    :param api_key: Kimi Code API Key
    :return: API 原始 JSON（dict）
    :raises RuntimeError: HTTP 状态码非 200
    :raises Exception: 网络错误、超时、JSON 解析失败等
    """
    req = urllib.request.Request(
        API_URL, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        if resp.status != 200:
            raise RuntimeError(f"API 返回状态码 {resp.status}")
        return json.loads(resp.read().decode("utf-8"))


def extract_usage_info(data):
    """把 API 原始 JSON 规整为统一的信息字典。

    从 "usage" 提取周配额三项；从 "limits" 中找到 5 小时（300 分钟）
    滚动窗口的剩余额度；从 "user.membership.level" 提取会员等级。

    :param data: fetch_usage 返回的原始 JSON
    :return: {"limit", "remaining", "reset_time",
              "window_remaining", "membership"}，缺失字段为 None
    """
    # 周配额信息
    weekly = data.get("usage", {}) or {}
    # 在 limits 数组中查找 5 小时滚动窗口（duration=300 分钟）
    window_detail = None
    for item in data.get("limits", []) or []:
        w = item.get("window", {}) or {}
        if w.get("timeUnit") == "TIME_UNIT_MINUTE" and w.get("duration") == 300:
            window_detail = item.get("detail", {}) or {}
            break
    return {
        "limit": weekly.get("limit"),
        "remaining": weekly.get("remaining"),
        "reset_time": weekly.get("resetTime"),
        "window_remaining": window_detail.get("remaining") if window_detail else None,
        "membership": (data.get("user", {}) or {}).get(
            "membership", {}).get("level", ""),
    }


def compute_used_percent(usage):
    """计算周用量百分比。

    :param usage: 含 "limit" 与 "remaining" 键的字典（值可为字符串，API 如此返回）
    :return: 用量百分比（float，如 26.0 表示 26%）；limit<=0 时返回 0.0 防除零
    """
    limit = float(usage["limit"])
    remaining = float(usage["remaining"])
    if limit <= 0:
        return 0.0
    return (limit - remaining) / limit * 100
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m unittest tests.test_kimi_watchdog -v`
Expected: 16 tests PASS

- [ ] **Step 5: 提交**

`.gcm.txt` 内容：

```
feat: 实现用量接口请求、数据规整与百分比计算
```

Run: `git add kimi_watchdog.py tests/test_kimi_watchdog.py && git commit -F .gcm.txt && del .gcm.txt`
Expected: 提交成功

---

### Task 4: 通知发送（Server酱 + QQ邮箱 + 分发）

**Files:**
- Modify: `kimi_watchdog.py`
- Modify: `tests/test_kimi_watchdog.py`

**Interfaces:**
- Produces:
  - `send_serverchan(sendkey, title, body) -> bool`：推送成功返回 True，任何失败打印警告并返回 False
  - `send_email(email_cfg, subject, body) -> bool`：`email_cfg` 为 `{"smtp_host", "smtp_port", "username", "auth_code", "to"}`
  - `notify_all(cfg, title, body) -> list[tuple[str, bool]]`：按配置分发到已启用渠道，返回 `[(渠道名, 是否成功)]`；失败渠道不影响其他渠道

- [ ] **Step 1: 写失败测试**

在 `tests/test_kimi_watchdog.py` 的 `if __name__ == "__main__":` 之前追加：

```python
EMAIL_CFG = {
    "smtp_host": "smtp.qq.com",
    "smtp_port": 465,
    "username": "from@qq.com",
    "auth_code": "authcode",
    "to": "to@qq.com",
}


class TestSendServerChan(unittest.TestCase):
    """send_serverchan：Server酱推送"""

    def test_ok(self):
        """Server酱返回 code==0 视为成功"""
        resp = _FakeResponse({"code": 0, "message": ""})
        with mock.patch.object(kw.urllib.request, "urlopen", return_value=resp):
            self.assertTrue(kw.send_serverchan("SCT123", "标题", "正文"))

    def test_api_error_code(self):
        """Server酱返回非 0 code 视为失败，但不抛异常"""
        resp = _FakeResponse({"code": 40001, "message": "bad sendkey"})
        with mock.patch.object(kw.urllib.request, "urlopen", return_value=resp):
            self.assertFalse(kw.send_serverchan("SCT123", "标题", "正文"))

    def test_network_error(self):
        """网络异常不抛出，返回 False"""
        with mock.patch.object(kw.urllib.request, "urlopen",
                               side_effect=OSError("timeout")):
            self.assertFalse(kw.send_serverchan("SCT123", "标题", "正文"))


class TestSendEmail(unittest.TestCase):
    """send_email：QQ邮箱 SMTP 发送"""

    def test_ok(self):
        """正常发送：登录并调用 sendmail"""
        fake_smtp = mock.MagicMock()
        with mock.patch.object(kw.smtplib, "SMTP_SSL", return_value=fake_smtp):
            self.assertTrue(kw.send_email(EMAIL_CFG, "主题", "正文"))
        fake_smtp.login.assert_called_once_with("from@qq.com", "authcode")
        fake_smtp.sendmail.assert_called_once()
        # 收件人正确
        self.assertEqual(fake_smtp.sendmail.call_args[0][1], ["to@qq.com"])

    def test_smtp_error(self):
        """SMTP 抛异常不向外传播，返回 False"""
        with mock.patch.object(kw.smtplib, "SMTP_SSL",
                               side_effect=smtplib.SMTPException("fail")):
            self.assertFalse(kw.send_email(EMAIL_CFG, "主题", "正文"))


class TestNotifyAll(unittest.TestCase):
    """notify_all：多渠道分发"""

    def test_both_channels(self):
        """两个渠道都配置时都调用"""
        cfg = {"serverchan_sendkey": "SCT123", "email": EMAIL_CFG}
        with mock.patch.object(kw, "send_serverchan", return_value=True) as ms, \
                mock.patch.object(kw, "send_email", return_value=True) as me:
            results = kw.notify_all(cfg, "标题", "正文")
        self.assertEqual(results, [("Server酱", True), ("QQ邮箱", True)])
        ms.assert_called_once()
        me.assert_called_once()

    def test_serverchan_failure_does_not_block_email(self):
        """Server酱失败不影响邮件渠道"""
        cfg = {"serverchan_sendkey": "SCT123", "email": EMAIL_CFG}
        with mock.patch.object(kw, "send_serverchan", return_value=False), \
                mock.patch.object(kw, "send_email", return_value=True) as me:
            results = kw.notify_all(cfg, "标题", "正文")
        self.assertEqual(results, [("Server酱", False), ("QQ邮箱", True)])
        me.assert_called_once()

    def test_no_channels(self):
        """未配置任何渠道返回空列表"""
        self.assertEqual(kw.notify_all({}, "标题", "正文"), [])
```

同时在该测试文件顶部 import 区域追加（若尚无）：

```python
import smtplib
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest tests.test_kimi_watchdog -v`
Expected: 新增 7 个测试 FAIL/ERROR，原 16 个仍 PASS

- [ ] **Step 3: 写最小实现**

在 `kimi_watchdog.py` 文件末尾追加：

```python
# ==================== 通知发送 ====================
def send_serverchan(sendkey, title, body):
    """通过 Server酱 推送到微信。

    :param sendkey: Server酱的 SendKey
    :param title: 通知标题（最长 32 字，服务端截断）
    :param body: 通知正文（Markdown）
    :return: True=发送成功，False=失败（已打印警告，不抛异常）
    """
    url = SERVERCHAN_URL.format(sendkey)
    data = urllib.parse.urlencode({"title": title, "desp": body}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            # Server酱约定 code==0 表示成功
            if result.get("code") == 0:
                return True
            print(f"[警告] Server酱返回错误: {result}")
            return False
    except Exception as e:
        print(f"[警告] Server酱发送失败: {e}")
        return False


def send_email(email_cfg, subject, body):
    """通过 QQ邮箱 SMTP（SSL 465 端口）发送邮件。

    :param email_cfg: 邮件配置 {"smtp_host", "smtp_port",
                      "username", "auth_code", "to"}
    :param subject: 邮件主题
    :param body: 邮件正文（纯文本）
    :return: True=发送成功，False=失败（已打印警告，不抛异常）
    """
    # 组装 MIME 邮件（UTF-8 编码，避免中文乱码）
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("kimi-watchdog", email_cfg["username"]))
    msg["To"] = email_cfg["to"]
    try:
        # 使用 SSL 直连（QQ邮箱 465 端口）
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(email_cfg["smtp_host"], email_cfg["smtp_port"],
                              timeout=HTTP_TIMEOUT, context=ctx) as server:
            server.login(email_cfg["username"], email_cfg["auth_code"])
            server.sendmail(email_cfg["username"], [email_cfg["to"]],
                            msg.as_string())
        return True
    except Exception as e:
        print(f"[警告] 邮件发送失败: {e}")
        return False


def notify_all(cfg, title, body):
    """按配置分发通知到所有已启用渠道。

    单渠道失败只记录结果，不影响其他渠道。

    :param cfg: load_config 返回的配置字典
    :param title: 通知标题
    :param body: 通知正文
    :return: [(渠道名, 是否成功)] 列表；未配置任何渠道时为空列表
    """
    results = []
    # 渠道一：Server酱
    if cfg.get("serverchan_sendkey"):
        ok = send_serverchan(cfg["serverchan_sendkey"], title, body)
        results.append(("Server酱", ok))
    # 渠道二：QQ邮箱
    if cfg.get("email"):
        ok = send_email(cfg["email"], title, body)
        results.append(("QQ邮箱", ok))
    return results
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m unittest tests.test_kimi_watchdog -v`
Expected: 23 tests PASS

- [ ] **Step 5: 提交**

`.gcm.txt` 内容：

```
feat: 实现 Server酱与 QQ邮箱双渠道通知及分发逻辑
```

Run: `git add kimi_watchdog.py tests/test_kimi_watchdog.py && git commit -F .gcm.txt && del .gcm.txt`
Expected: 提交成功

---

### Task 5: 通知消息构建

**Files:**
- Modify: `kimi_watchdog.py`
- Modify: `tests/test_kimi_watchdog.py`

**Interfaces:**
- Consumes: `extract_usage_info` 的返回结构（Task 3）、`compute_used_percent`（Task 3）
- Produces:
  - `format_reset_time(iso_str) -> str`：ISO 时间（含 `Z` 后缀）转本地时间 `YYYY-MM-DD HH:MM`，解析失败返回原串
  - `build_message(reason, info) -> (title, body)`：`info` 为 `extract_usage_info` 返回值；返回通知标题与正文（正文含触发原因、本周用量/剩余/百分比、重置时间、5小时窗口剩余、会员等级）

- [ ] **Step 1: 写失败测试**

在 `tests/test_kimi_watchdog.py` 的 `if __name__ == "__main__":` 之前追加：

```python
class TestFormatResetTime(unittest.TestCase):
    """format_reset_time：ISO 时间转可读格式"""

    def test_iso_with_z(self):
        # Z 后缀的 UTC 时间可正常解析并转换为本地时区
        result = kw.format_reset_time("2026-02-11T17:32:50.757941Z")
        self.assertRegex(result, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_invalid_returns_original(self):
        """解析失败返回原字符串"""
        self.assertEqual(kw.format_reset_time("garbage"), "garbage")

    def test_none_returns_placeholder(self):
        """None 返回'未知'"""
        self.assertEqual(kw.format_reset_time(None), "未知")


class TestBuildMessage(unittest.TestCase):
    """build_message：通知内容组装"""

    def test_content(self):
        info = kw.extract_usage_info(SAMPLE_API_DATA)
        title, body = kw.build_message("本周用量已达 26.0%（阈值 25%）", info)
        self.assertIn("本周用量已达 26.0%", title)
        self.assertIn("触发原因", body)
        self.assertIn("26 / 100", body)          # 已用 / 总量
        self.assertIn("26.0%", body)             # 百分比
        self.assertIn("剩余：74", body)           # 剩余额度
        self.assertIn("2026-02-11", body)         # 重置时间（本地时区日期）
        self.assertIn("5小时窗口剩余：85", body)   # 滚动窗口
        self.assertIn("LEVEL_INTERMEDIATE", body) # 会员等级
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest tests.test_kimi_watchdog -v`
Expected: 新增 4 个测试 FAIL/ERROR，原 23 个仍 PASS

- [ ] **Step 3: 写最小实现**

在 `kimi_watchdog.py` 文件末尾追加：

```python
# ==================== 通知消息构建 ====================
def format_reset_time(iso_str):
    """把 API 返回的 ISO 时间（UTC，Z 后缀）转为本地可读时间。

    :param iso_str: 如 "2026-02-11T17:32:50.757941Z"，可能为 None
    :return: "YYYY-MM-DD HH:MM" 本地时间；解析失败返回原串；None 返回 "未知"
    """
    if not iso_str:
        return "未知"
    try:
        # Python 3.8 的 fromisoformat 不认 "Z"，需替换为 "+00:00"
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_str


def build_message(reason, info):
    """组装通知标题与正文。

    :param reason: 触发原因描述（如 "本周用量已达 80.0%（阈值 80%）"）
    :param info: extract_usage_info 返回的用量信息字典
    :return: (title, body) 二元组
    """
    limit = info.get("limit")
    remaining = info.get("remaining")
    # 百分比与已用量（API 返回字符串，需转数值）
    pct = compute_used_percent({"limit": limit, "remaining": remaining})
    used = float(limit) - float(remaining)

    title = f"【Kimi额度提醒】{reason}"
    lines = [
        f"触发原因：{reason}",
        "",
        f"本周用量：{used:g} / {limit}（{pct:.1f}%）",
        f"本周剩余：{remaining}",
        f"重置时间：{format_reset_time(info.get('reset_time'))}",
    ]
    # 5 小时滚动窗口剩余（可能缺失）
    if info.get("window_remaining") is not None:
        lines.append(f"5小时窗口剩余：{info['window_remaining']}")
    # 会员等级（可能缺失）
    if info.get("membership"):
        lines.append(f"会员等级：{info['membership']}")
    lines += ["", f"—— kimi-watchdog 于 {datetime.now():%Y-%m-%d %H:%M:%S}"]
    return title, "\n".join(lines)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m unittest tests.test_kimi_watchdog -v`
Expected: 27 tests PASS

- [ ] **Step 5: 提交**

`.gcm.txt` 内容：

```
feat: 实现通知消息组装与重置时间格式化
```

Run: `git add kimi_watchdog.py tests/test_kimi_watchdog.py && git commit -F .gcm.txt && del .gcm.txt`
Expected: 提交成功

---

### Task 6: 命令行入口与主循环

**Files:**
- Modify: `kimi_watchdog.py`
- Modify: `tests/test_kimi_watchdog.py`

**Interfaces:**
- Consumes: 前面所有任务的函数（签名见各任务 Produces）
- Produces: `main(argv=None) -> int`（退出码）；`if __name__ == "__main__": sys.exit(main())`

- [ ] **Step 1: 写失败测试**

在 `tests/test_kimi_watchdog.py` 的 `if __name__ == "__main__":` 之前追加：

```python
# 主循环测试用的基础配置：只启用 Server酱，间隔 0 秒
LOOP_CFG = {"api_key": "sk-test", "poll_interval_sec": 0,
            "serverchan_sendkey": "SCT123", "email": None}


def _make_usage_data(remaining):
    """构造指定剩余额度的 API 返回数据（limit 固定 100）"""
    return {
        "usage": {"limit": "100", "remaining": remaining,
                  "resetTime": "2099-01-01T00:00:00Z"},
        "limits": [], "user": {"membership": {"level": "L1"}},
    }


class TestMainArgs(unittest.TestCase):
    """命令行参数校验"""

    def test_monitor_mode_requires_both_args(self):
        """监控模式缺少参数时报错退出（argparse 的 error 走 exit code 2）"""
        with mock.patch.object(kw, "load_config", return_value=dict(LOOP_CFG)):
            with self.assertRaises(SystemExit):
                kw.main(["80"])

    def test_missing_api_key(self):
        """未配置 API Key 时以 EXIT_ERROR 退出"""
        with mock.patch.object(kw, "load_config",
                               return_value={"api_key": "", "poll_interval_sec": 600,
                                             "serverchan_sendkey": "", "email": None}):
            self.assertEqual(kw.main(["80", "2099-12-31 23:59"]), kw.EXIT_ERROR)

    def test_no_notify_channel(self):
        """未配置任何通知渠道时以 EXIT_ERROR 退出"""
        cfg = {"api_key": "sk", "poll_interval_sec": 600,
               "serverchan_sendkey": "", "email": None}
        with mock.patch.object(kw, "load_config", return_value=cfg):
            self.assertEqual(kw.main(["80", "2099-12-31 23:59"]), kw.EXIT_ERROR)


class TestMainLoop(unittest.TestCase):
    """主循环触发逻辑（mock 掉 sleep / fetch_usage / notify_all）"""

    def _run(self, argv, fetch=None):
        """运行 main 的公共封装：mock 配置、sleep、通知"""
        with mock.patch.object(kw, "load_config", return_value=dict(LOOP_CFG)), \
                mock.patch.object(kw.time, "sleep"), \
                mock.patch.object(kw, "notify_all") as mock_notify, \
                mock.patch.object(kw, "fetch_usage", fetch):
            code = kw.main(argv)
        return code, mock_notify

    def test_quota_trigger(self):
        """用量 95% >= 阈值 90% -> EXIT_QUOTA，通知一次"""
        code, mock_notify = self._run(["90", "2099-12-31 23:59"],
                                      fetch=mock.Mock(return_value=_make_usage_data("5")))
        self.assertEqual(code, kw.EXIT_QUOTA)
        mock_notify.assert_called_once()
        # 标题包含触发信息
        title = mock_notify.call_args[0][1]
        self.assertIn("本周用量", title)

    def test_quota_not_reached_polls_again(self):
        """未达阈值时继续轮询：前 2 次 50%，第 3 次 5% 触发退出"""
        side_effects = [_make_usage_data("50"), _make_usage_data("50"),
                        _make_usage_data("5")]
        fetch = mock.Mock(side_effect=side_effects)
        code, _ = self._run(["90", "2099-12-31 23:59"], fetch=fetch)
        self.assertEqual(code, kw.EXIT_QUOTA)
        self.assertEqual(fetch.call_count, 3)  # 说明发生了多轮轮询

    def test_time_trigger(self):
        """到达指定时刻 -> EXIT_TIME"""
        code, mock_notify = self._run(["90", "2000-01-01 00:00"],
                                      fetch=mock.Mock(return_value=_make_usage_data("50")))
        self.assertEqual(code, kw.EXIT_TIME)
        mock_notify.assert_called_once()

    def test_consecutive_failures_exit_error(self):
        """API 连续 5 次失败 -> 发异常通知并以 EXIT_ERROR 退出"""
        fetch = mock.Mock(side_effect=RuntimeError("boom"))
        code, mock_notify = self._run(["90", "2099-12-31 23:59"], fetch=fetch)
        self.assertEqual(code, kw.EXIT_ERROR)
        self.assertEqual(fetch.call_count, kw.MAX_CONSECUTIVE_FAILURES)
        mock_notify.assert_called_once()  # 发送"监控异常"通知

    def test_failure_counter_resets(self):
        """失败计数在成功后清零：失败4次、成功1次、再失败5次才退出"""
        side_effects = [RuntimeError("e")] * 4 + [_make_usage_data("50")] \
            + [RuntimeError("e")] * kw.MAX_CONSECUTIVE_FAILURES
        fetch = mock.Mock(side_effect=side_effects)
        code, _ = self._run(["90", "2099-12-31 23:59"], fetch=fetch)
        self.assertEqual(code, kw.EXIT_ERROR)
        # 4 失败 + 1 成功 + 5 失败 = 10 次调用
        self.assertEqual(fetch.call_count, 10)


class TestTestNotifyMode(unittest.TestCase):
    """--test-notify 模式"""

    def test_sends_test_notification(self):
        """发送测试通知并返回 EXIT_OK"""
        cfg = dict(LOOP_CFG)
        with mock.patch.object(kw, "load_config", return_value=cfg), \
                mock.patch.object(kw, "notify_all",
                                  return_value=[("Server酱", True)]) as mock_notify:
            code = kw.main(["--test-notify"])
        self.assertEqual(code, kw.EXIT_OK)
        mock_notify.assert_called_once()
        self.assertIn("测试", mock_notify.call_args[0][1])

    def test_partial_failure_returns_error(self):
        """任一渠道失败 -> EXIT_ERROR"""
        cfg = dict(LOOP_CFG)
        with mock.patch.object(kw, "load_config", return_value=cfg), \
                mock.patch.object(kw, "notify_all",
                                  return_value=[("Server酱", False)]):
            code = kw.main(["--test-notify"])
        self.assertEqual(code, kw.EXIT_ERROR)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest tests.test_kimi_watchdog -v`
Expected: 新增 10 个测试 FAIL/ERROR（`kw.main` 不存在），原 27 个仍 PASS

- [ ] **Step 3: 写最小实现**

在 `kimi_watchdog.py` 文件末尾追加：

```python
# ==================== 命令行入口与主循环 ====================
def main(argv=None):
    """命令行入口：解析参数、加载配置，进入主循环或测试通知模式。

    :param argv: 命令行参数列表（测试时注入），默认 sys.argv[1:]
    :return: 进程退出码（见模块 docstring 的退出码约定）
    """
    parser = argparse.ArgumentParser(
        description="Kimi Code 周额度监控：用量达阈值或到指定时刻时，"
                    "通过 Server酱/QQ邮箱 通知后退出。")
    parser.add_argument("percent", nargs="?", type=float,
                        help="周用量百分比阈值，如 80 表示用量达 80%% 触发")
    parser.add_argument("deadline", nargs="?",
                        help="目标时刻，如 18:00（已过则明天）或 2026-09-03 18:00")
    parser.add_argument("--test-notify", action="store_true",
                        help="仅发送测试通知验证渠道连通性，不做监控")
    parser.add_argument("--config", default=CONFIG_FILE,
                        help=f"配置文件路径（默认 {CONFIG_FILE}）")
    args = parser.parse_args(argv)

    # ---- 加载与校验配置 ----
    cfg = load_config(args.config)

    # --test-notify 模式：只发测试通知
    if args.test_notify:
        if not (cfg.get("serverchan_sendkey") or cfg.get("email")):
            print("错误：未配置任何通知渠道，请检查 config.json")
            return EXIT_ERROR
        print("正在发送测试通知...")
        results = notify_all(cfg, "【Kimi额度监控】测试通知",
                             "这是一条 kimi-watchdog 测试通知，收到即说明渠道配置正确。")
        return EXIT_OK if all(ok for _, ok in results) else EXIT_ERROR

    # 监控模式：两个位置参数必填
    if args.percent is None or args.deadline is None:
        parser.error("监控模式必须同时提供 <percent> 和 <time> 两个参数")

    # 校验 API Key 与通知渠道
    if not cfg.get("api_key"):
        print("错误：未配置 API Key（config.json 的 api_key 或环境变量 KIMI_API_KEY）")
        return EXIT_ERROR
    if not (cfg.get("serverchan_sendkey") or cfg.get("email")):
        print("错误：未配置任何通知渠道，请检查 config.json")
        return EXIT_ERROR

    # 解析目标时刻
    try:
        deadline = parse_deadline(args.deadline)
    except ValueError as e:
        print(f"错误：{e}")
        return EXIT_ERROR

    interval = int(cfg.get("poll_interval_sec", 600))
    print(f"开始监控：阈值 {args.percent}%，目标时刻 {deadline:%Y-%m-%d %H:%M}，"
          f"轮询间隔 {interval} 秒。按 Ctrl+C 停止。")

    failures = 0  # API 连续失败计数（成功一次即清零）
    try:
        while True:
            now = datetime.now()

            # ---- 条件一：到达指定时刻 ----
            # 触发前尽力再取一次用量数据用于报告；取不到也不影响触发
            if now >= deadline:
                try:
                    info = extract_usage_info(fetch_usage(cfg["api_key"]))
                    title, body = build_message(
                        f"已到达指定时刻 {deadline:%Y-%m-%d %H:%M}", info)
                except Exception:
                    title = "【Kimi额度提醒】已到达指定时刻"
                    body = (f"触发原因：已到达指定时刻 {deadline:%Y-%m-%d %H:%M}\n"
                            f"（触发时用量数据获取失败）")
                _report(notify_all(cfg, title, body), f"已到达指定时刻 {deadline:%Y-%m-%d %H:%M}")
                return EXIT_TIME

            # ---- 轮询用量 ----
            try:
                data = fetch_usage(cfg["api_key"])
                info = extract_usage_info(data)
                pct = compute_used_percent(
                    {"limit": info["limit"], "remaining": info["remaining"]})
                failures = 0  # 成功，清零连续失败计数
            except Exception as e:
                failures += 1
                print(f"[警告] 第 {failures} 次 API 请求失败: {e}")
                # 连续失败达上限：通知监控异常并退出
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    _report(notify_all(
                        cfg, "【Kimi额度监控】监控异常",
                        f"API 连续 {failures} 次请求失败，监控已退出。\n最后错误：{e}"),
                        "监控异常")
                    return EXIT_ERROR
                # 未达上限：睡到下轮继续重试（但不越过 deadline）
                _sleep_until_next(interval, deadline)
                continue

            print(f"[{now:%H:%M:%S}] 周用量 {pct:.1f}%"
                  f"（剩余 {info['remaining']}/{info['limit']}）")

            # ---- 条件二：用量达到阈值 ----
            if pct >= args.percent:
                reason = f"本周用量已达 {pct:.1f}%（阈值 {args.percent:g}%）"
                title, body = build_message(reason, info)
                _report(notify_all(cfg, title, body), reason)
                return EXIT_QUOTA

            # 未触发：睡到下轮（不越过 deadline，保证时刻触发准时）
            _sleep_until_next(interval, deadline)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，监控已停止。")
        return EXIT_OK


def _sleep_until_next(interval, deadline):
    """睡到下一轮轮询，但睡眠总时长不超过 deadline（保证时刻触发不迟到）。"""
    remaining = (deadline - datetime.now()).total_seconds()
    time.sleep(max(1, min(interval, remaining)))


def _report(results, reason):
    """打印通知发送结果摘要。"""
    if not results:
        print(f"[{reason}] 未配置任何通知渠道（理论不应发生）。")
        return
    for name, ok in results:
        print(f"[{reason}] {name} 通知{'成功' if ok else '失败'}")


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m unittest tests.test_kimi_watchdog -v`
Expected: 37 tests PASS

若 `test_quota_not_reached_polls_again` 等主循环测试卡死，检查 `_sleep_until_next` 中 `time.sleep` 是否被正确 mock（测试 mock 的是 `kw.time.sleep`，实现中必须通过 `time.sleep(...)` 方式调用）。

- [ ] **Step 5: 手动冒烟测试**

Run: `python kimi_watchdog.py --help`
Expected: 打印用法帮助，包含 `percent`、`deadline`、`--test-notify`、`--config`

Run: `python kimi_watchdog.py 80`
Expected: 报错提示"监控模式必须同时提供 <percent> 和 <time> 两个参数"，退出码 2

- [ ] **Step 6: 提交**

`.gcm.txt` 内容：

```
feat: 实现命令行入口、监控主循环与退出码约定
```

Run: `git add kimi_watchdog.py tests/test_kimi_watchdog.py && git commit -F .gcm.txt && del .gcm.txt`
Expected: 提交成功

---

### Task 7: README 与最终验收

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: 全部已完成功能

- [ ] **Step 1: 写 README**

`README.md` 完整内容：

```markdown
# kimi-watchdog

Kimi Code API 周额度监控脚本：当本周用量达到设定阈值，或到达指定时刻时，
通过 **Server酱（微信推送）** 和 **QQ邮箱** 发送提醒通知，然后退出。

零依赖，仅需 Python 3.8+。

## 快速开始

1. 复制配置模板并填写：

   ```
   copy config.example.json config.json
   ```

   - `api_key`：Kimi Code 的 API Key（也可用环境变量 `KIMI_API_KEY`，优先级更高）
   - `serverchan_sendkey`：[Server酱](https://sct.ftqq.com/) 的 SendKey，留空则不启用
   - `email`：QQ邮箱 SMTP 配置（需在 QQ邮箱设置中开启 SMTP 并获取授权码），整块留空则不启用

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
| `email` | 空 | QQ邮箱 SMTP 配置（smtp_host/smtp_port/username/auth_code/to） |

## 运行测试

```
python -m unittest tests.test_kimi_watchdog -v
```
```

- [ ] **Step 2: 全量测试**

Run: `python -m unittest tests.test_kimi_watchdog -v`
Expected: 37 tests PASS, 0 FAIL

- [ ] **Step 3: 验收清单逐项核对**

- `python kimi_watchdog.py --help` 输出正常
- `python kimi_watchdog.py 80`（缺参数）报错退出码 2
- `python kimi_watchdog.py 80 abc`（时间非法）打印"无法解析时间参数"且退出码 3
- 代码内无 TODO/占位符，全部函数有中文 docstring

- [ ] **Step 4: 提交**

`.gcm.txt` 内容：

```
docs: 添加 README 使用说明
```

Run: `git add README.md && git commit -F .gcm.txt && del .gcm.txt`
Expected: 提交成功

---

## 任务依赖关系

Task 1（骨架/配置）→ Task 2（时间解析）→ Task 3（用量获取）→ Task 4（通知）→ Task 5（消息构建）→ Task 6（主循环，汇总前面全部）→ Task 7（README/验收）。

顺序执行，无并行空间（单文件项目，后一任务依赖前一任务的函数）。
