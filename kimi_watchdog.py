#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kimi-watchdog：Kimi Code API 周额度监控脚本

功能：
    启动时传入「用量百分比阈值」和「目标时刻」两个参数，脚本常驻轮询
    Kimi Code 用量接口；任一条件满足时，通过 Server酱（微信推送）
    发送提醒通知，然后退出。

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
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

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
        serverchan_sendkey Server酱 SendKey（空字符串表示未配置）
    """
    # 默认配置
    cfg = {
        "api_key": "",
        "poll_interval_sec": 600,
        "serverchan_sendkey": "",
    }
    # 配置文件存在则覆盖默认值
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    # 环境变量优先级最高
    if os.environ.get("KIMI_API_KEY"):
        cfg["api_key"] = os.environ["KIMI_API_KEY"]
    return cfg


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
