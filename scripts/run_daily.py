#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端入口：一次跑完签到 + 猫猫旅行，并把结果推送到微信。

顺序与容错（对应需求）：
  1. 先跑签到（claim）。
  2. 再跑成长中心/猫猫旅行（growth）：脚本内部已实现
     「先领已到家的旅行积分 → 再判断能否派新一趟 → 今日已派过则不派」。
  3. 两段各自 try，任何一段失败都不阻断另一段；
     最终退出码只看签到，签到成功即成功。
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SIGNIN = os.path.join(REPO_ROOT, "signin.py")


def run_signin(cmd):
    """运行上游签到脚本，返回原始 stdout 文本。"""
    try:
        proc = subprocess.run(
            [sys.executable, SIGNIN, cmd],
            capture_output=True, text=True,
            timeout=int(os.environ.get("WB_TIMEOUT", "300")),
        )
        return proc.stdout.strip()
    except subprocess.TimeoutExpired:
        return json.dumps({"result": "TIMEOUT", "report": "执行超时", "needs_attention": True})
    except Exception as exc:
        return json.dumps({"result": "ERROR", "report": str(exc), "needs_attention": True})


def main():
    # ---------- 1. 签到 ----------
    checkin_raw = run_signin("claim")
    print("[run] 签到完成")

    # ---------- 2. 猫猫旅行 ----------
    # 失败只记录，不影响签到结论
    try:
        growth_raw = run_signin("growth")
    except Exception as exc:
        growth_raw = json.dumps(
            {"result": "ERROR", "report": f"成长中心异常：{exc}", "needs_attention": True}
        )
    print("[run] 成长中心完成")

    # ---------- 3. 落盘原始返回，便于排查（不含明文 token） ----------
    out_dir = os.path.join(REPO_ROOT, "artifacts")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "checkin.json"), "w", encoding="utf-8") as fh:
        fh.write(checkin_raw)
    with open(os.path.join(out_dir, "growth.json"), "w", encoding="utf-8") as fh:
        fh.write(growth_raw)

    # ---------- 4. 推送 ----------
    notify = os.path.join(HERE, "push.py")
    proc = subprocess.run(
        [sys.executable, notify, checkin_raw, growth_raw],
        capture_output=True, text=True,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()