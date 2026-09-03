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


if __name__ == "__main__":
    unittest.main()
