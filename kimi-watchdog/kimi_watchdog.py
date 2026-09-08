#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kimi-watchdog：Kimi Code API 周额度监控脚本

功能：
    启动时传入「用量百分比阈值」和「目标时刻」两个参数，脚本常驻轮询
    Kimi Code 用量接口；任一条件满足时，通过 Server酱（微信推送）
    发送提醒通知，然后退出。
    额度阈值触发时，若配置了 refresh_token，自动删除 config 中 api_key
    对应的 API Key 后通知退出。
    另提供 --test-delete 干跑模式：刷新+匹配并打印将删除的 Key，
    但不真正删除，用于配置后自检。

用法：
    python kimi_watchdog.py <percent> <time> [--test-notify] [--test-delete]
    例：python kimi_watchdog.py 80 18:00

退出码：
    0 = 正常退出（含 --test-notify/--test-delete 成功、Ctrl+C 主动停止）
    1 = 额度阈值触发
    2 = 指定时刻触发
    3 = 监控异常（API 连续 5 次请求失败）；测试通知/干跑失败
    4 = 额度阈值触发，但删除链路失败
"""

import argparse
import base64
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

# ---------------- 常量定义 ----------------
API_URL = "https://api.kimi.com/coding/v1/usages"  # Kimi Code 用量查询接口（非官方，社区逆向）
CONFIG_FILE = "config.json"                        # 默认配置文件路径
SERVERCHAN_URL = "https://sctapi.ftqq.com/{}.send"  # Server酱推送接口，{} 处填 sendkey
# 网页版控制台接口（非官方，逆向自 kimi.com 前端；与 sk- API Key 不同体系）
REFRESH_URL = "https://www.kimi.com/api/auth/token/refresh"
LIST_KEYS_URL = "https://www.kimi.com/apiv2/kimi.gateway.credentials.v1.APIKeyService/ListAPIKeys"
DELETE_KEY_URL = "https://www.kimi.com/apiv2/kimi.gateway.credentials.v1.APIKeyService/DeleteAPIKey"
HTTP_TIMEOUT = 10                                   # HTTP 请求超时（秒）
MAX_CONSECUTIVE_FAILURES = 5                        # API 连续失败多少次后判定监控异常

# 退出码约定（见模块 docstring）
EXIT_OK = 0
EXIT_QUOTA = 1
EXIT_TIME = 2
EXIT_ERROR = 3
EXIT_DELETE_FAILED = 4  # 额度触发但删除链路失败


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


# ==================== 命令行入口与主循环 ====================
def main(argv=None):
    """命令行入口：解析参数、加载配置，进入主循环或测试通知模式。

    :param argv: 命令行参数列表（测试时注入），默认 sys.argv[1:]
    :return: 进程退出码（见模块 docstring 的退出码约定）
    """
    parser = argparse.ArgumentParser(
        description="Kimi Code 周额度监控：用量达阈值或到指定时刻时，"
                    "通过 Server酱（微信推送）通知后退出。")
    parser.add_argument("percent", nargs="?", type=float,
                        help="周用量百分比阈值，如 80 表示用量达 80%% 触发")
    parser.add_argument("deadline", nargs="?",
                        help="目标时刻，如 18:00（已过则明天）或 2026-09-03 18:00")
    parser.add_argument("--test-notify", action="store_true",
                        help="仅发送测试通知验证渠道连通性，不做监控")
    parser.add_argument("--test-delete", action="store_true",
                        help="删除链路干跑：刷新+匹配并打印将删除的 Key，但不真正删除")
    parser.add_argument("--config", default=CONFIG_FILE,
                        help=f"配置文件路径（默认 {CONFIG_FILE}）")
    args = parser.parse_args(argv)

    # ---- 加载与校验配置 ----
    cfg = load_config(args.config)

    # ---- refresh_token 有效期预检：只警告，不阻断监控 ----
    if cfg.get("refresh_token"):
        days = check_refresh_token_expiry(cfg["refresh_token"])
        if days is None:
            print("[警告] refresh_token 无法解析（不是合法 JWT），删除功能将不可用")
        elif days < 0:
            print("[警告] refresh_token 已过期，删除功能将不可用，请重新抓取")
        elif days < 7:
            print(f"[提醒] refresh_token 剩余有效期约 {days:.1f} 天，建议尽快重新抓取")

    # --test-notify 模式：只发测试通知
    if args.test_notify:
        if not cfg.get("serverchan_sendkey"):
            print("错误：未配置 serverchan_sendkey，请检查 config.json")
            return EXIT_ERROR
        print("正在发送测试通知...")
        ok = send_serverchan(cfg["serverchan_sendkey"], "【Kimi额度监控】测试通知",
                             "这是一条 kimi-watchdog 测试通知，收到即说明渠道配置正确。")
        return EXIT_OK if ok else EXIT_ERROR

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

    # 监控模式：两个位置参数必填
    if args.percent is None or args.deadline is None:
        parser.error("监控模式必须同时提供 <percent> 和 <time> 两个参数")

    # 校验 API Key 与通知渠道
    if not cfg.get("api_key"):
        print("错误：未配置 API Key（config.json 的 api_key 或环境变量 KIMI_API_KEY）")
        return EXIT_ERROR
    if not cfg.get("serverchan_sendkey"):
        print("错误：未配置 serverchan_sendkey，请检查 config.json")
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
                _send_and_print(cfg, title, body,
                                f"已到达指定时刻 {deadline:%Y-%m-%d %H:%M}")
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
                    _send_and_print(
                        cfg, "【Kimi额度监控】监控异常",
                        f"API 连续 {failures} 次请求失败，监控已退出。\n最后错误：{e}",
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
                exit_code = EXIT_QUOTA
                # 配置了 refresh_token 才启用自动删除；未配置时行为与原来一致
                if cfg.get("refresh_token"):
                    ok, detail = _attempt_key_deletion(cfg, args.config)
                    body += f"\n\nKey 删除：{detail}"
                    if not ok:
                        exit_code = EXIT_DELETE_FAILED
                _send_and_print(cfg, title, body, reason)
                return exit_code

            # 未触发：睡到下轮（不越过 deadline，保证时刻触发准时）
            _sleep_until_next(interval, deadline)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，监控已停止。")
        return EXIT_OK


def _sleep_until_next(interval, deadline):
    """睡到下一轮轮询，但睡眠总时长不超过 deadline（保证时刻触发不迟到）。"""
    remaining = (deadline - datetime.now()).total_seconds()
    time.sleep(max(1, min(interval, remaining)))


def _send_and_print(cfg, title, body, reason):
    """发送 Server酱 通知并打印结果摘要。

    :param cfg: 配置字典
    :param title: 通知标题
    :param body: 通知正文
    :param reason: 触发原因（用于控制台打印）
    """
    ok = send_serverchan(cfg["serverchan_sendkey"], title, body)
    print(f"[{reason}] Server酱 通知{'成功' if ok else '失败'}")


if __name__ == "__main__":
    sys.exit(main())
