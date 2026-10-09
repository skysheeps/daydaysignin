#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把签到与猫猫旅行结果推送到微信相关通道。

重要前提
--------
个人微信（WeChat）**没有开放的主动推送接口**，任何"推送到个人微信"的方案
都必须经过一个中间通道。本脚本支持以下几种，按你已有的任选一个配置即可：

  1. 企业微信机器人   WECHAT_WORK_WEBHOOK   —— 配置最简单，一个地址即可，群内直接收消息
  2. Server 酱 Turbo  SERVERCHAN_KEY       —— 通过服务号推送到你的微信，无需企业微信
  3. PushPlus         PUSHPLUS_TOKEN       —— 微信/公众号推送
  4. WxPusher         WXPUSHER_APPTOKEN + WXPUSHER_UID

哪个 secret 存在就用哪个；都没配时只打印预览（方便排查），不影响结论。

行为保证（沿用原约束）
  * 签到与猫猫状态分开成区块。
  * 猫猫失败不影响签到结论，退出码只由签到决定。
  * 所有文本经 redact() 擦除凭证。
  * 推送失败只记录日志，绝不改变业务结论。
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request

# 签到成功的判定：上游把结果放在 result，或放在 body.code
# code=10001 表示"今天已签到"，属幂等成功（接口幂等，不会重复领取）
CHECKIN_OK = {"CLAIMED", "ALREADY_CHECKED_IN", "OK", "SUCCESS"}
CHECKIN_OK_CODES = {0, 10001}

# 各通道的内容长度上限（字节），超出截断，避免被服务端拒绝
CONTENT_LIMIT = {
    "wechat_work": 4096,
    "serverchan": 32000,
    "pushplus": 32000,
    "wxpusher": 4000,
}


def mask(value, keep=4):
    """脱敏：只保留前后少量字符，绝不回显完整凭证。"""
    if not value:
        return "<empty>"
    v = str(value)
    if len(v) <= keep * 2:
        return "*" * len(v)
    return f"{v[:keep]}{'*' * 6}{v[-keep:]}"


def redact(text):
    """对来自服务端的文本做凭证擦除。

    上游 report/msg 由服务端拼装，理论上不夹带 token；但推送是外部通道，
    一旦服务端异常回显完整凭证就会外泄。这里再兜一层，宁可误伤也不外泄。
    """
    if not text:
        return text
    s = str(text)
    # JWT 形态：三段base64 以点分隔
    s = re.sub(r"\beyJ[A-Za-z0-9._~+/-]{16,}={0,2}\b", "[JWT已脱敏]", s)
    # 长串 base64/hex（>=40 字符），典型即凭证
    s = re.sub(r"\b[A-Za-z0-9._~+/-]{40,}={0,2}\b", "[长凭证已脱敏]", s)
    # 显式键值对
    s = re.sub(
        r"(?i)\b(access_?token|refresh_?token|authorization|bearer|token)\b\s*[:=]?\s*\S{12,}",
        lambda m: f"{m.group(1)}=[已脱敏]",
        s,
    )
    return s


def is_checkin_ok(data):
    """判定签到是否成功，覆盖 result 与 body.code 两种返回形态。"""
    if str(data.get("result", "")).upper() in CHECKIN_OK:
        return True
    code = data.get("body", {}).get("code")
    if code in CHECKIN_OK_CODES:
        return True
    return False


def parse_result(raw):
    """上游脚本输出为单行 JSON，这里做安全解析。"""
    raw = (raw or "").strip()
    if not raw:
        return {"result": "EMPTY", "report": "脚本无输出", "needs_attention": True}
    try:
        return json.loads(raw)
    except Exception:
        return {"result": "UNPARSEABLE", "report": raw[:400], "needs_attention": True}


def checkin_block(data):
    """构建签到区块。结论独立，不受猫猫影响。"""
    ok = is_checkin_ok(data)
    body = data.get("body", {}) or {}
    code = body.get("code")

    if ok and code == 10001:
        verdict = "✅ 成功（今日已签到）"
    elif ok:
        verdict = "✅ 成功（已领取）"
    else:
        verdict = "❌ 失败"

    lines = [f"**签到**：{verdict}"]
    if not ok and body.get("msg"):
        lines.append(f"· 服务端提示：{redact(body['msg'])}")

    info = body.get("data") or {}
    if not info and ok:
        lines.append("· 说明：接口幂等，今日已领取，未重复发放")
    if info:
        if info.get("streak_days") is not None:
            lines.append(f"· 连续签到：{info['streak_days']} 天")
        if info.get("today_credit") is not None:
            lines.append(f"· 今日获得：{info['today_credit']} 积分")
        if info.get("total_credits") is not None:
            lines.append(f"· 活动累计：{info['total_credits']} 积分")
    elif not ok and data.get("report"):
        lines.append(f"· 说明：{redact(data['report'])}")
    return "\n".join(lines), ok


def growth_block(data):
    """构建猫猫（Buddy）区块。失败只在此体现，不上抛。"""
    res = str(data.get("result", "")).upper()
    if data.get("needs_attention") or res in (
        "ERROR", "AUTH_REJECTED", "FORBIDDEN", "TIMEOUT", "NETWORK"
    ):
        verdict = "⚠️ 未完成"
    elif res == "GROWTH":
        verdict = "✅ 已处理"
    else:
        verdict = "ℹ️ 无需处理"

    lines = [f"**猫猫 Buddy 旅行**：{verdict}"]
    report = data.get("report")
    if data.get("idle") is True:
        lines.append("· 当前状态：空闲，可派新一趟")
    elif data.get("idle") is False:
        lines.append("· 当前状态：旅行中 / 处理中，未派新一趟")
    if report:
        lines.append(f"· 详情：{redact(report)}")
    if data.get("credits_gained"):
        lines.append(f"· 本轮积分：+{data['credits_gained']}")
    if data.get("energy") is not None:
        lines.append(f"· 能量：{data['energy']}")
    return "\n".join(lines)


def build_text(checkin, growth):
    """组装消息体。两个区块用分隔线隔开，便于手机端阅读。

    分隔线用全角横线而非 Markdown 的 ---，因为企业微信机器人不渲染
    Markdown 水平分割线，会原样显示成三个减号。
    """
    cb, ok = checkin_block(checkin)
    gb = growth_block(growth)
    title = "Buddy 加油站 · 每日签到"
    body = f"{cb}\n\n——————————\n\n{gb}"
    return title, body, ok


def truncate(text, limit):
    """按字节截断，避免超出通道上限。"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return text.encode("utf-8")[: limit - 30].decode("utf-8", "ignore") + "\n…（内容过长已截断）"


def _post(url, data=None, json_body=None, headers=None):
    """统一的 POST，失败只抛异常由上层兜住。"""
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        hdr = {"Content-Type": "application/json"}
    else:
        body = urllib.parse.urlencode(data or {}).encode("utf-8")
        hdr = {"Content-Type": "application/x-www-form-urlencoded"}
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdr, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def push_wechat_work(webhook, title, content):
    """企业微信群机器人：地址即含 key。"""
    content = truncate(content, CONTENT_LIMIT["wechat_work"])
    status, raw = _post(webhook, json_body={"msgtype": "markdown", "markdown": {"content": content}})
    ok = status == 200 and ("errcode" in raw and '"errcode":0' in raw.replace(" ", ""))
    return ok, f"HTTP {status} {raw[:120]}"


def push_serverchan(key, title, content):
    """Server 酱Turbo：推送到你的微信服务号，无需企业微信。"""
    content = truncate(content, CONTENT_LIMIT["serverchan"])
    status, raw = _post(
        f"https://sctapi.ftqq.com/{urllib.parse.quote(key)}.send",
        data={"title": title, "desp": content},
    )
    return status == 200, f"HTTP {status} {raw[:120]}"


def push_pushplus(token, title, content):
    """PushPlus：微信/公众号推送。"""
    content = truncate(content, CONTENT_LIMIT["pushplus"])
    status, raw = _post(
        "https://www.pushplus.plus/send",
        json_body={"token": token, "title": title, "content": content.replace("\n", "<br>")},
    )
    return status == 200 and '"code":200' in raw.replace(" ", ""), f"HTTP {status} {raw[:120]}"


def push_wxpusher(app_token, uid, title, content):
    """WxPusher：公众号推送，content 支持 Markdown。"""
    content = truncate(content, CONTENT_LIMIT["wxpusher"])
    summary = title if len(title) <= 100 else title[:100]
    status, raw = _post(
        "https://wxpusher.zjiecode.com/api/send/message",
        json_body={
            "appToken": app_token, "uid": uid,
            "content": content, "summary": summary,
        },
    )
    return status == 200 and '"code":1000' in raw.replace(" ", ""), f"HTTP {status} {raw[:120]}"


def dispatch(title, content):
    """按已配置的 secret 自动选择通道并发送。"""
    attempts = []

    if os.environ.get("WECHAT_WORK_WEBHOOK", "").strip():
        attempts.append(("企业微信机器人", lambda: push_wechat_work(
            os.environ["WECHAT_WORK_WEBHOOK"].strip(), title, content)))
    if os.environ.get("SERVERCHAN_KEY", "").strip():
        attempts.append(("Server酱", lambda: push_serverchan(
            os.environ["SERVERCHAN_KEY"].strip(), title, content)))
    if os.environ.get("PUSHPLUS_TOKEN", "").strip():
        attempts.append(("PushPlus", lambda: push_pushplus(
            os.environ["PUSHPLUS_TOKEN"].strip(), title, content)))
    if os.environ.get("WXPUSHER_APPTOKEN", "").strip() and os.environ.get("WXPUSHER_UID", "").strip():
        attempts.append(("WxPusher", lambda: push_wxpusher(
            os.environ["WXPUSHER_APPTOKEN"].strip(),
            os.environ["WXPUSHER_UID"].strip(), title, content)))

    if not attempts:
        print("[push] 未配置任何推送通道 secret，跳过发送。")
        return False

    all_ok = True
    for name, fn in attempts:
        try:
            ok, detail = fn()
            print(f"[push] {name}: {'成功' if ok else '失败'} | {detail}")
            all_ok = all_ok and ok
        except Exception as exc:
            # 推送失败绝不改变业务结论
            print(f"[push] {name}: 异常（不影响签到结论）：{exc}", file=sys.stderr)
            all_ok = False
    return all_ok


def is_truthy(name):
    v = (os.environ.get(name) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def should_push(checkin, growth):
    """决定是否值得打扰用户。

    一天跑 6 次，若每次都推送会变成骚扰。静默规则（WB_QUIET=1 时生效）：
      · 签到失败或需要关注 → 必须推送（异常要让人知道）
      · 本轮真的领到积分/能量 → 推送（有收益才值得看）
      · 其余情况（今日已签到、旅行中、无事发生）→ 静默，只留日志
    """
    if not is_truthy("WB_QUIET"):
        return True, "非静默模式"

    if not is_checkin_ok(checkin):
        return True, "签到失败，需要关注"
    if checkin.get("needs_attention"):
        return True, "签到返回需关注状态"

    gained = growth.get("credits_gained") or 0
    if gained > 0:
        return True, f"本轮领到 {gained} 积分"

    # credits_gained 只统计部分来源（如任务奖）。旅行礼物与派出发写在
    # report 文本里，故对关键进展做文本识别，避免被静默吞掉。
    report = str(growth.get("report") or "")
    for kw, why in (
        ("领旅行礼物", "领到旅行礼物"),
        ("派 Buddy", "已派出新一趟"),
        ("开 Buddy 盲盒", "开了盲盒"),
        ("连登奖励", "连登奖励已兑换"),
    ):
        if kw in report:
            return True, why

    # 注意：energy 是「开盲盒后的余额」，不是本轮增量，
    # 上游未提供增量字段，故不能用它判断是否有新收益。
    return False, "无新收益，静默"


def main():
    checkin_raw = sys.argv[1] if len(sys.argv) > 1 else ""
    growth_raw = sys.argv[2] if len(sys.argv) > 2 else ""

    checkin = parse_result(checkin_raw)
    growth = parse_result(growth_raw)
    title, text, ok = build_text(checkin, growth)

    print("[push] 签到结论：", "成功" if ok else "失败")
    print("[push] 猫猫区块已独立生成（不影响签到结论）")

    push_it, reason = should_push(checkin, growth)
    print(f"[push] 推送决策：{'推送' if push_it else '静默'}（{reason}）")

    # 无论推不推，日志都完整打印，便于排查
    print("---- 消息内容 ----")
    print(f"{title}\n{text}")

    if push_it:
        dispatch(title, text)
    else:
        print("[push] 已静默：本次不打扰，仅保留 Actions 日志。")

    # 退出码只由签到决定：签到成功就算成功
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()