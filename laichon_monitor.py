#!/usr/bin/env python3
"""
name: 莱充低功率提醒
cron: */5 * * * *

青龙任务命令：task laichon_monitor.py
环境变量：LAICHON_TOKEN、LAICHON_OPENID、SHOWDOC_PUSH_URL。
第二账号可选：LAICHON_TOKEN_2、LAICHON_OPENID_2。
"""

import fcntl
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


BASE_URL = "https://saas.laichon.com/api/app/ebike/order/"
CHINA = timezone(timedelta(hours=8))
MAX_AGE = 15 * 60


class APIError(RuntimeError):
    """莱充请求失败；只包含可以安全写入日志及通知的说明。"""


def configured_accounts():
    accounts = []
    for suffix, default_name in (("", "我的车"), ("_2", "老婆的车")):
        token = os.getenv("LAICHON_TOKEN" + suffix, "").strip()
        openid = os.getenv("LAICHON_OPENID" + suffix, "").strip()
        if suffix and not token and not openid:
            continue
        if not token or not openid:
            raise RuntimeError(f"请成对配置 LAICHON_TOKEN{suffix} 和 LAICHON_OPENID{suffix}")
        name = os.getenv("LAICHON_NAME" + suffix, "").strip() or default_name
        accounts.append((suffix, name, token, openid))
    return accounts


def post_api(endpoint, payload, token, openid):
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token.split(None, 1)[1]
    request = Request(
        BASE_URL + endpoint,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": "bearer " + token,
            "openid": openid,
            "Service-Code": "WYC-MI-WEIXIN",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise APIError(f"{endpoint} 接口 HTTP {exc.code}；若为 401/403，请更新登录凭证") from None
    except (URLError, TimeoutError, OSError):
        raise APIError(f"{endpoint} 接口连接失败，请检查网络") from None
    except (ValueError, UnicodeError):
        raise APIError(f"{endpoint} 接口响应格式异常") from None
    if not isinstance(result, dict) or result.get("code") != 0:
        # 不输出响应内容，避免接口返回登录凭证或其他个人信息。
        raise APIError(f"{endpoint} 接口返回业务错误，请检查登录凭证")
    if not isinstance(result.get("data"), dict):
        raise APIError(f"{endpoint} 接口 data 格式异常")
    return result["data"]


def query_months(now):
    current = datetime.fromtimestamp(now, CHINA)
    previous = current.replace(day=1) - timedelta(days=1)
    return [current.strftime("%Y-%m"), previous.strftime("%Y-%m")]


def active_orders(call, now):
    orders = {}
    for month in query_months(now):
        data = call("list", {"search_by": {"month_of_year": month, "order_status": ""}})
        items = data.get("list")
        if not isinstance(items, list):
            raise APIError("订单列表格式异常")
        for item in items:
            if str(item.get("order_status")) == "3":
                number = str(item.get("order_no") or "")
                if not number:
                    raise APIError("充电中的订单缺少订单号")
                orders[number] = item
    return orders


def latest_samples(detail):
    history = detail.get("charging_details")
    if not isinstance(history, dict):
        raise ValueError("没有带时间的功率记录，等待下一次更新")
    start = float(history["startTimestamp"])
    keys = sorted((int(key) for key in history if key.isdigit()))
    samples = []
    for offset in keys[-2:]:
        entry = history[str(offset)]
        power = float(entry["real_power"])
        timestamp = start + offset
        if not math.isfinite(power) or power < 0 or not math.isfinite(timestamp):
            raise ValueError("功率记录包含无效数值")
        samples.append((timestamp, power))
    if not samples:
        raise ValueError("没有功率采样，等待下一次更新")
    return start, samples


def inspect_order(detail, record, threshold, now):
    """仅使用最后两条真实采样；重复、陈旧和间隔异常的数据不能触发。"""
    if str(detail.get("order_status")) != "3":
        return False, "订单已经不在充电中", None
    start, samples = latest_samples(detail)
    latest, power = samples[-1]
    if now - latest > MAX_AGE or latest - now > 120:
        return False, "采样时间异常或超过 15 分钟未更新", None
    if latest - start < 600:
        return False, "充电开始不足 10 分钟", latest
    if record.get("notified"):
        return False, f"功率 {power:g} W，本订单已提醒", latest
    if latest <= record.get("last_sample", -1) and record.get("threshold") == threshold:
        return False, "没有新的功率采样", latest
    if len(samples) < 2:
        return False, "等待第二条功率采样", latest
    gap = latest - samples[-2][0]
    if not 240 <= gap <= 420:
        return False, "最近两条采样间隔异常，暂不触发", latest
    ready = all(value < threshold for _, value in samples)
    message = f"最近两条功率：{samples[-2][1]:g} → {power:g} W"
    return ready, message, latest


def send_notification(title, content):
    url = os.getenv("SHOWDOC_PUSH_URL", "").strip()
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != "push.showdoc.com.cn"
        or not parts.path.startswith("/server/api/push/")
        or not parts.path.removeprefix("/server/api/push/")
        or parts.query
        or parts.fragment
    ):
        raise RuntimeError("请在 SHOWDOC_PUSH_URL 中填写完整的 ShowDoc 专属推送地址")
    request = Request(
        url,
        data=urlencode({"title": title, "content": content}).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"ShowDoc 推送 HTTP {exc.code}，下次执行会重试") from None
    except (URLError, TimeoutError, OSError):
        # urllib 异常可能包含带令牌的 URL，不让原文进入日志。
        raise RuntimeError("ShowDoc 推送连接失败，下次执行会重试") from None
    except (ValueError, UnicodeError):
        raise RuntimeError("ShowDoc 推送响应格式异常，下次执行会重试") from None
    if not isinstance(result, dict) or result.get("error_code") != 0:
        raise RuntimeError("ShowDoc 推送返回失败，请检查推送地址；下次执行会重试")


def load_state(path):
    if not path.exists():
        return {}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or any(not isinstance(value, dict) for value in state.values()):
        raise ValueError("状态文件格式异常；请检查文件，避免丢失提醒记录")
    return state


def save_state(path, state):
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run(call, notify, state_path, threshold, now, account_name="我的车"):
    state = load_state(state_path)
    orders = active_orders(call, now)
    print(f"[{account_name}] 找到 {len(orders)} 个充电中订单；提醒阈值 < {threshold:g} W")
    errors = 0
    api_error = None
    for number in orders:
        record = state.get(number, {})
        try:
            detail = call("detail", {"order_no": number})
            if str(detail.get("order_no")) != number:
                raise ValueError("详情订单号与请求不一致")
            ready, message, timestamp = inspect_order(detail, record, threshold, now)
            print(f"[{account_name}] 订单 {number}：{message}")
            if ready:
                content = "\n".join([
                    f"车辆：{account_name}",
                    f"订单：{number}",
                    f"地点：{detail.get('site_name', '')} {detail.get('device_name', '')}",
                    f"设备：{detail.get('device_no', '')}，插口：{detail.get('gun_no', '')}",
                    message,
                    f"采样时间：{datetime.fromtimestamp(timestamp, CHINA):%Y-%m-%d %H:%M:%S}",
                    f"功率连续两条采样低于 {threshold:g} W，已进入低功率阶段，请查看充电情况。",
                    "低功率不代表已充满或已断电。",
                ])
                notify(f"{account_name}：莱充低功率提醒", content)
                record["notified"] = True
                print("ShowDoc 已接受推送请求")
            if timestamp is not None:
                record.update(last_sample=timestamp, threshold=threshold, seen_at=now)
                state[number] = record
                save_state(state_path, state)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
            # 通知失败时不提交本次采样，下一轮仍可重试。
            errors += 1
            if isinstance(exc, APIError):
                api_error = exc
            print(f"[{account_name}] 订单 {number}：处理失败（{type(exc).__name__}），详情：{exc}")
    if api_error is not None:
        raise api_error
    return 1 if errors else 0


def report_api_error(state_path, account_name, error):
    state = load_state(state_path)
    if state.get("_api_error", {}).get("notified"):
        print(f"[{account_name}] 接口仍异常，本次不重复推送")
        return
    content = "\n".join([
        f"车辆：{account_name}",
        f"异常：{error}",
        "暂时无法查询充电状态，低功率监控无法正常进行。",
        "若登录已失效，请重新抓包更新此账号的 TOKEN 和 OPENID；连接失败时请检查网络。",
        "本次异常只提醒一次，查询恢复后会自动重新启用异常提醒。",
    ])
    send_notification(f"{account_name}：莱充接口请求异常", content)
    state["_api_error"] = {"notified": True}
    save_state(state_path, state)
    print(f"[{account_name}] ShowDoc 已接受接口异常推送")


def clear_api_error(state_path):
    state = load_state(state_path)
    if "_api_error" in state:
        del state["_api_error"]
        save_state(state_path, state)


def main():
    if sys.argv[1:] == ["--test-notify"]:
        send_notification("莱充提醒测试", "ShowDoc 推送配置测试。正式监控将在连续两条功率低于 20 W 时提醒。")
        print("ShowDoc 已接受测试推送，请查看微信")
        return 0
    if sys.argv[1:]:
        raise ValueError("仅支持无参数监控或 --test-notify 测试推送")
    accounts = configured_accounts()
    if not os.getenv("SHOWDOC_PUSH_URL", "").strip():
        raise RuntimeError("请在青龙环境变量中配置 SHOWDOC_PUSH_URL")
    threshold = float(os.getenv("LAICHON_POWER_THRESHOLD", "20"))
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("LAICHON_POWER_THRESHOLD 必须是大于 0 的有限数值")
    state_path = Path(os.getenv("LAICHON_STATE_FILE", str(Path(__file__).with_name(".laichon_state.json"))))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("另一次监控仍在执行，跳过本次")
            return 0
        status = 0
        for suffix, name, token, openid in accounts:
            account_state = state_path.with_name(state_path.stem + suffix + state_path.suffix)
            call = lambda endpoint, payload: post_api(endpoint, payload, token, openid)
            try:
                result = run(call, send_notification, account_state, threshold, time.time(), name)
                status = max(status, result)
                if result == 0:
                    clear_api_error(account_state)
            except APIError as exc:
                status = 1
                print(f"[{name}] {exc}")
                try:
                    report_api_error(account_state, name, exc)
                except (RuntimeError, ValueError, OSError) as push_error:
                    print(f"[{name}] 异常提醒未完成（{type(push_error).__name__}），下次重试")
            except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
                status = 1
                print(f"[{name}] 监控失败（{type(exc).__name__}），请检查配置与状态文件")
        return status


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # 不输出异常原文，避免第三方通知错误将令牌或通知地址写进日志。
        print(f"监控执行失败：{type(exc).__name__}。请检查环境变量、接口凭证、网络及状态文件。")
        sys.exit(1)
