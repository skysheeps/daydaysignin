#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把签到与猫猫旅行结果发送到你的邮箱。

需要的 secret（仓库 Settings → Secrets and variables → Actions 里配）：
  MAIL_HOST      SMTP 服务器，如 smtp.qq.com / smtp.163.com / smtp.gmail.com
  MAIL_PORT      端口：QQ/163 用 465（隐式 SSL），Gmail 用 587（STARTTLS）
  MAIL_USER      发件账号（写完整地址）
  MAIL_PASSWORD  授权码，**不是登录密码**
  MAIL_TO        收件地址，可与发件人相同（可选，默认同 MAIL_USER）
  MAIL_FROM_NAME 发件人显示名（可选）

注意：QQ/163 需在邮箱设置中开启 SMTP 服务并生成「授权码」；
Gmail 需开启两步验证并创建 App Password。登录密码在云端 runner 上
基本都会被服务商拒绝，务必用授权码。

行为保证（沿用原约束）
  * 签到与猫猫状态分开成区块。
  * 猫猫失败不影响签到结论，退出码只由签到决定。
  * 所有文本经 redact() 擦除凭证。
  * 发送失败只记录日志，绝不改变业务结论。
"""

import json
import os
import re
import smtplib
import socket
import ssl
import sys
from email.message import EmailMessage

# 签到成功的判定：上游把结果放在 result，或放在 body.code
# code=10001 表示"今天已签到"，属幂等成功（接口幂等，不会重复领取）
CHECKIN_OK = {"CLAIMED", "ALREADY_CHECKED_IN", "OK", "SUCCESS"}
CHECKIN_OK_CODES = {0, 10001}

# 邮件正文长度上限（字符），超出截断，避免被邮件服务商拒收
CONTENT_LIMIT = {"email": 20000}


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

    上游 report/msg 由服务端拼装，理论上不夹带 token；但邮件是外部通道，
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


def send_email(title, content):
    """通过 SMTP 发一封纯文本邮件到你的邮箱。

    需要的 secret（全部在仓库 Settings → Actions 里配）：
      MAIL_HOST      SMTP 服务器地址，如 smtp.qq.com / smtp.163.com
      MAIL_PORT      端口，QQ/163=465，Gmail=587
      MAIL_USER      发件账号（完整地址）
      MAIL_PASSWORD  授权码，**不是登录密码**
      MAIL_TO        收件地址（可与发件人相同）

    云端 runner 连的是公共 SMTP，不存在本机安全软件拦截，因此不需要
    任何本地端口放行。关键是用授权码：QQ/163 需在邮箱设置里开启 SMTP
    服务并生成授权码；Gmail 需开两步验证并建 App Password。

    GitHub runner 的两个坑已在此处处理：
      1. IPv6：托管 runner 会解析到 IPv6 但容器内无 IPv6 路由，
         直接连会报 [Errno 101] Network is unreachable —— 故强制走 IPv4。
      2. 端口：465 在部分 runner 上不可达，会自动降级到 587（STARTTLS），
         反之亦然。两个都失败时把两个端口的真实报错一起抛出，便于定位。
    """
    host = os.environ["MAIL_HOST"].strip()
    user = os.environ["MAIL_USER"].strip()
    password = os.environ["MAIL_PASSWORD"].strip()
    to = os.environ.get("MAIL_TO", "").strip() or user
    sender_name = os.environ.get("MAIL_FROM_NAME", "WorkBuddy签到助手").strip()

    message = EmailMessage()
    message["Subject"] = f"[Buddy加油站] {title}"
    message["From"] = f"{sender_name} <{user}>"
    message["To"] = to
    message.set_content(f"{title}\n\n{'=' * 40}\n\n{content}\n")

    # 端口优先级：配置的端口优先，然后 465 / 587。
    # 25 与 2525 一律跳过：那是明文提交口，GitHub 托管 runner 与多数邮箱
    # 服务商都已禁用或强限流，用它们只会白等 30 秒超时。
    configured = os.environ.get("MAIL_PORT", "").strip()
    order = []
    if configured:
        try:
            p = int(configured)
            if p in (465, 587):
                order.append(p)
        except ValueError:
            pass  # 端口填错就按默认顺序走，不因此中断
    order += [p for p in (465, 587) if p not in order]

    errors = []
    for port in order:
        try:
            _smtp_send(host, port, user, password, message)
            return True, f"已发送至 {to}（via {host}:{port}）"
        except Exception as exc:
            errors.append(f"{port}端口: {type(exc).__name__}: {exc}")

    raise RuntimeError("；".join(errors))


def _smtp_send(host, port, user, password, message):
    """按端口建立 SMTP 连接并发送。

    host 必须始终传主机名，绝不能换成 IP。
    TLS 的 SNI 与证书校验都依赖主机名：163 邮箱的证书只对 *.163.com 有效，
    用 IP 直连必然报 "certificate is not valid for '1.2.3.4'"。
    规避 runner 无 IPv6 路由的正确做法是「把域名解析限制为 IPv4」，
    而不是「把主机名替换为 IP」。
    """
    ctx = ssl.create_default_context()

    # 临时把 getaddrinfo 限定为 IPv4：GitHub runner 会解析到 IPv6 但容器内
    # 没有 IPv6 路由，直连报 [Errno 101]。只改解析族，主机名保持不变。
    original_getaddrinfo = socket.getaddrinfo

    def ipv4_only(host_, port_, family=0, type=0, proto=0, flags=0):
        return original_getaddrinfo(host_, port_, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = ipv4_only
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=30, context=ctx)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls(context=ctx)
    finally:
        socket.getaddrinfo = original_getaddrinfo

    try:
        server.login(user, password)
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def dispatch(title, content):
    """按已配置的 secret 选择发送方式。"""
    attempts = []

    if os.environ.get("MAIL_HOST", "").strip() and os.environ.get("MAIL_USER", "").strip():
        attempts.append(("邮件", lambda: send_email(title, content)))

    if not attempts:
        print("[push] 未配置 MAIL_HOST/MAIL_USER，跳过发送。")
        return False

    all_ok = True
    for name, fn in attempts:
        try:
            ok, detail = fn()
            print(f"[push] {name}: {'成功' if ok else '失败'} | {detail}")
            all_ok = all_ok and ok
        except Exception as exc:
            # 发送失败绝不改变业务结论
            print(f"[push] {name}: 异常（不影响签到结论）：{exc}", file=sys.stderr)
            all_ok = False
    return all_ok


def is_truthy(name):
    v = (os.environ.get(name) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def should_push(checkin, growth):
    """决定是否值得打扰用户（决定这封邮件要不要发）。

    一天跑 6 次，若每次都发信会变成骚扰。静默规则（WB_QUIET=1 时生效）：
      · 签到失败或需要关注 → 必须发信（异常要让人知道）
      · 本轮真的领到积分/能量 → 发信（有收益才值得看）
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
    print(f"[push] 发送决策：{'发信' if push_it else '静默'}（{reason}）")

    # 无论发不发，日志都完整打印，便于排查
    print("---- 邮件内容 ----")
    print(f"{title}\n{text}")

    if push_it:
        dispatch(title, text)
    else:
        print("[push] 已静默：本次不打扰，仅保留 Actions 日志。")

    # 退出码只由签到决定：签到成功就算成功
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()