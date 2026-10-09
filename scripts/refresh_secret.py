#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从本机 WorkBuddy 登录态导出签到凭证，并写入 GitHub Actions secret。

设计要点（对应需求约束）：
  * 云端 runner 没有登录态，凭证只能由本机脚本注入 secret。
  * 明文 token 绝不写入仓库、绝不打印到终端/日志，只经由 stdin 传给 gh。
  * 解释器路径不写死版本号：用 command -v 动态探测（本脚本内亦不硬编码 python3.x）。

前置条件：
  - 本机已登录 WorkBuddy 桌面端。
  - 已安装 gh CLI 并完成认证（gh auth login）。
  - 已在仓库配置好secret 名（默认 WB_AUTH_FILE）。

用法：
  python3 refresh_secret.py --repo 你名/你的仓库
  python3 refresh_secret.py --repo 你名/你的仓库 --dry-run   # 只打印脱敏预览
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 登录态文件的候选位置（与桌面端实际存储布局一致）。
AUTH_BASENAME = os.path.join(
    "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info"
)


def find_auth_file():
    """定位本机 WorkBuddy 登录态文件。

    优先读环境变量 WORKBUDDY_AUTH_FILE（便于显式覆盖），
    否则按 Windows / macOS / Linux 的已知路径探测。
    """
    override = os.environ.get("WORKBUDDY_AUTH_FILE")
    if override:
        p = Path(override)
        return p if p.is_file() else None

    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    xdg_data = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    candidates = [
        os.path.join(local, AUTH_BASENAME),                             # Windows 桌面端
        os.path.join(home, "Library", "Application Support", AUTH_BASENAME),  # macOS
        os.path.join(xdg_data, "CodeBuddyExtension", "Data", "Public", "auth",
                     "workbuddy-desktop.info"),                        # Linux CLI
    ]
    for c in candidates:
        if os.path.isfile(c):
            return Path(c)
    return None


def mask(value, keep=4):
    """脱敏：只保留前后少量字符，绝不回显完整 token。"""
    if not value:
        return "<empty>"
    v = str(value)
    if len(v) <= keep * 2:
        return v[0] + "*" * (len(v) - 1)
    return f"{v[:keep]}{'*' * 6}{v[-keep:]}"


def extract(path):
    """从登录态文件提取 accessToken 与 uid。

    仅明文凭据可直接导出；若为 sym-v1 加密信封，说明客户端版本较新，
    需在客户端内解密，此处明确失败而不是产出不可用凭证。
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    auth = (data.get("auth") or {})
    account = (data.get("account") or {})
    token = auth.get("accessToken")
    uid = account.get("uid")
    if not token or not uid:
        raise SystemExit("登录态文件结构异常：缺少 accessToken 或 uid")

    # 判定是否为明文 token：加密信封是 {"$wbEncrypted":1,"envelope":...}
    if isinstance(token, dict) or "envelope" in str(token)[:200]:
        raise SystemExit(
            "检测到加密凭据（sym-v1）。该形态需在 WorkBuddy 运行时内解密，"
            "无法在无客户端环境下导出明文。请先在客户端重新登录以刷新凭据。"
        )
    return {
        "accessToken": token,
        "uid": uid,
        "enterpriseId": account.get("enterpriseId") or "",
        "domain": auth.get("domain") or "",
    }


def to_auth_payload(cred):
    """组装成 signin.py 期望的 session 结构。

    上游脚本读取 WORKBUDDY_AUTH_FILE 指向的 JSON，其中
    auth.accessToken / account.uid / account.enterpriseId / auth.domain
    会被 build_headers() 用于构造鉴权头。
    """
    return {
        "auth": {
            "accessToken": cred["accessToken"],
            "domain": cred["domain"],
        },
        "account": {
            "uid": cred["uid"],
            "enterpriseId": cred["enterpriseId"],
        },
    }


def main():
    ap = argparse.ArgumentParser(description="导出 WorkBuddy 签到凭证到 GitHub secret")
    ap.add_argument("--repo", required=True, help="目标仓库，形如 owner/name")
    ap.add_argument("--secret", default="WB_AUTH_FILE",
                    help="secret 名称（默认 WB_AUTH_FILE）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印脱敏预览，不写入 secret")
    ap.add_argument("--emit", action="store_true",
                    help="打印 base64 串供复制到 GitHub 网页（无需 gh CLI）")
    args = ap.parse_args()

    path = find_auth_file()
    if path is None:
        raise SystemExit(
            "未找到 WorkBuddy 登录态文件。请确认桌面端已登录，"
            "或用 WORKBUDDY_AUTH_FILE 指定 workbuddy-desktop.info 的路径。"
        )
    cred = extract(path)
    payload = to_auth_payload(cred)

    # 只输出脱敏信息，证明脚本走到了正确位置且格式正确。
    print("[refresh] 登录态文件 :", path)
    print("[refresh] uid        :", cred["uid"])
    print("[refresh] token      :", mask(cred["accessToken"]))
    print("[refresh] enterprise  :", cred["enterpriseId"] or "<none>")

    if args.dry_run:
        print("[refresh] dry-run：未写入 secret。")
        return

    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")

    # --emit：只把 base64 串打到终端，供你复制到 GitHub 网页的 Secret 输入框。
    # 不需要 gh CLI，也不会把凭据写进任何文件。
    if args.emit:
        print()
        print("=" * 68)
        print("请复制下面这一整行，粘贴到 GitHub 仓库：")
        print("  Settings → Secrets and variables → Actions → New repository secret")
        print(f"  Secret 名称：{args.secret}")
        print("=" * 68)
        print(encoded)
        print("=" * 68)
        print("粘贴完成后清空终端历史（该串等价于你的登录凭证）。")
        return

    # 写入模式需要 gh CLI；没有就提示改用 --emit。
    if shutil.which("gh") is None:
        raise SystemExit(
            "未找到 gh CLI。二选一：\n"
            "  A) 加 --emit 参数，把打印出的字符串粘贴到 GitHub 网页（推荐，无需安装）\n"
            "  B) 安装 gh CLI 并执行 gh auth login 后重跑"
        )

    # 经 stdin 传给 gh，避免 token 出现在命令行参数与进程列表中。
    proc = subprocess.run(
        ["gh", "secret", "set", args.secret, "--repo", args.repo, "--body", "-"],
        input=encoded, text=True, capture_output=True,
    )
    if proc.returncode != 0:
        raise SystemExit("写入 secret 失败：", proc.stderr.strip())
    print(f"[refresh] 已写入 secret {args.secret}（base64 封装，仓库内无明文）。")


if __name__ == "__main__":
    main()