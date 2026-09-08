# -*- coding: utf-8 -*-
"""kimi_watchdog 单元测试（标准库 unittest，无需安装任何依赖）"""

import json
import os
import sys
import tempfile
import unittest
import urllib.error
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


# 主循环测试用的基础配置：启用 Server酱，轮询间隔 0 秒
LOOP_CFG = {"api_key": "sk-test", "poll_interval_sec": 0,
            "serverchan_sendkey": "SCT123"}


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
                                             "serverchan_sendkey": ""}):
            self.assertEqual(kw.main(["80", "2099-12-31 23:59"]), kw.EXIT_ERROR)

    def test_no_serverchan_key(self):
        """未配置 Server酱 SendKey 时以 EXIT_ERROR 退出"""
        cfg = {"api_key": "sk", "poll_interval_sec": 600,
               "serverchan_sendkey": ""}
        with mock.patch.object(kw, "load_config", return_value=cfg):
            self.assertEqual(kw.main(["80", "2099-12-31 23:59"]), kw.EXIT_ERROR)


class TestMainLoop(unittest.TestCase):
    """主循环触发逻辑（mock 掉 sleep / fetch_usage / send_serverchan）"""

    def _run(self, argv, fetch=None):
        """运行 main 的公共封装：mock 配置、sleep、通知"""
        with mock.patch.object(kw, "load_config", return_value=dict(LOOP_CFG)), \
                mock.patch.object(kw.time, "sleep"), \
                mock.patch.object(kw, "send_serverchan") as mock_send, \
                mock.patch.object(kw, "fetch_usage", fetch):
            code = kw.main(argv)
        return code, mock_send

    def test_quota_trigger(self):
        """用量 95% >= 阈值 90% -> EXIT_QUOTA，通知一次"""
        code, mock_send = self._run(["90", "2099-12-31 23:59"],
                                    fetch=mock.Mock(return_value=_make_usage_data("5")))
        self.assertEqual(code, kw.EXIT_QUOTA)
        mock_send.assert_called_once()
        # 标题包含触发信息
        title = mock_send.call_args[0][1]
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
        code, mock_send = self._run(["90", "2000-01-01 00:00"],
                                    fetch=mock.Mock(return_value=_make_usage_data("50")))
        self.assertEqual(code, kw.EXIT_TIME)
        mock_send.assert_called_once()

    def test_consecutive_failures_exit_error(self):
        """API 连续 5 次失败 -> 发异常通知并以 EXIT_ERROR 退出"""
        fetch = mock.Mock(side_effect=RuntimeError("boom"))
        code, mock_send = self._run(["90", "2099-12-31 23:59"], fetch=fetch)
        self.assertEqual(code, kw.EXIT_ERROR)
        self.assertEqual(fetch.call_count, kw.MAX_CONSECUTIVE_FAILURES)
        mock_send.assert_called_once()  # 发送"监控异常"通知

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
                mock.patch.object(kw, "send_serverchan", return_value=True) as mock_send:
            code = kw.main(["--test-notify"])
        self.assertEqual(code, kw.EXIT_OK)
        mock_send.assert_called_once()
        self.assertIn("测试", mock_send.call_args[0][1])

    def test_send_failure_returns_error(self):
        """发送失败 -> EXIT_ERROR"""
        cfg = dict(LOOP_CFG)
        with mock.patch.object(kw, "load_config", return_value=cfg), \
                mock.patch.object(kw, "send_serverchan", return_value=False):
            code = kw.main(["--test-notify"])
        self.assertEqual(code, kw.EXIT_ERROR)


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


if __name__ == "__main__":
    unittest.main()
