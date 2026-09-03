# -*- coding: utf-8 -*-
"""kimi_watchdog 单元测试（标准库 unittest，无需安装任何依赖）"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
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

    def test_load_from_file(self):
        """正常读取配置文件"""
        path = self._write_temp_config({
            "api_key": "sk-file",
            "poll_interval_sec": 300,
            "serverchan_sendkey": "SCT123",
        })
        cfg = kw.load_config(path)
        self.assertEqual(cfg["api_key"], "sk-file")
        self.assertEqual(cfg["poll_interval_sec"], 300)
        self.assertEqual(cfg["serverchan_sendkey"], "SCT123")

    def test_env_var_overrides_file(self):
        """环境变量 KIMI_API_KEY 优先于配置文件中的 api_key"""
        path = self._write_temp_config({"api_key": "sk-file"})
        with mock.patch.dict(os.environ, {"KIMI_API_KEY": "sk-env"}):
            cfg = kw.load_config(path)
        self.assertEqual(cfg["api_key"], "sk-env")


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
        # 重置时间：UTC 转 本地时区（与实现相同算法，保证测试与时区无关）
        expected_local = datetime.fromisoformat(
            "2026-02-11T17:32:50.757941+00:00").astimezone().strftime("%Y-%m-%d %H:%M")
        self.assertIn(expected_local, body)
        self.assertIn("5小时窗口剩余：85", body)   # 滚动窗口
        self.assertIn("LEVEL_INTERMEDIATE", body) # 会员等级


if __name__ == "__main__":
    unittest.main()
