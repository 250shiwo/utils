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


# ==================== 通知发送 ====================
def send_serverchan(sendkey, title, body):
    """通过 Server酱 推送到微信。

    Server酱是一个纯 HTTP POST 接口，无需安装任何 SDK：
    POST https://sctapi.ftqq.com/<sendkey>.send，表单参数 title 与 desp。

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
