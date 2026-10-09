# Buddy 加油站每日签到 + 猫猫旅行（GitHub Actions）

用 GitHub Actions 云端定时任务完成 WorkBuddy「Buddy 加油站」的每日签到与 Buddy 旅行领取，结果推送到微信。

## 真实接口（来自 88lin/workbuddy-auto-signin + 服务端实测，非网上旧接口名）

| 用途 | 方法 | 路径 |
|---|---|---|
| 查签到状态 | GET | `/v2/billing/meter/checkin-activity-status` |
| 领取签到积分 | POST | `/v2/billing/meter/daily-checkin` |
| 猫猫旅行状态 | GET | `/v2/activity/growth/buddy/travel/status` |
| 领取旅行积分 | POST | `/v2/activity/growth/buddy/travel/claim` |
| 旅行目的地 | GET | `/v2/activity/growth/buddy/travel/config` |
| 派Buddy 出发 | POST | `/v2/activity/growth/buddy/travel/depart` |

主机 `https://copilot.tencent.com`，鉴权 `Authorization: Bearer <token>` + `X-User-Id`。

## 行为保证

- **顺序**：先签到 → 再成长中心。成长中心内部先领已到家的旅行积分，再判断能否派新一趟；`daily_limit_reached` 为真时直接收手不撞墙。
- **容错**：签到与猫猫各自独立 try。**退出码只由签到决定**，猫猫崩溃不影响签到结论。
- **幂等**：`code=10001`（今日已签到）判定为成功，不重复领取。
- **脱敏**：所有推送文本经 `redact()` 擦除 JWT / 长串凭证，token 不进日志、不进仓库。

## 一次性配置

### 1. 推送到GitHub

```bash
cd wb-signin
git init && git add . && git commit -m "feat: Buddy 加油站每日签到 + 猫猫旅行"
git remote add origin https://github.com/<你的账号>/<仓库名>.git
git push -u origin main
```

### 2. 注入凭证 secret（在本机执行）

云端 runner 没有登录态，凭证必须由本机注入。

**方式 A · 网页粘贴（推荐，不需要装 gh）**

```bash
python3 scripts/refresh_secret.py --repo <你的账号>/<仓库名> --emit
```

会打印一行 base64 串，复制它，然后到仓库页面粘贴：

> Settings → Secrets and variables → Actions → **New repository secret**
> Name 填 `WB_AUTH_FILE`，Secret 填那一行 base64

**方式 B · 用 gh CLI 自动写入**

```bash
# 需先安装 gh 并执行 gh auth login
python3 scripts/refresh_secret.py --repo <你的账号>/<仓库名>
```

**先干跑确认能读到登录态**（两种方式都建议先跑）：

```bash
python3 scripts/refresh_secret.py --repo <你的账号>/<仓库名> --dry-run
```

写入的 secret 名为 `WB_AUTH_FILE`，内容是 base64 封装的 session JSON。
token 全程经 stdin 传给 `gh secret set`，不进命令行、不落仓库。

> ⚠️ 那行 base64 等价于你的登录凭证。粘贴完请清空终端历史（`clear`），
> 也别把它贴到聊天、issue 或截图中。泄露后处理办法见文末。

### 3. 配置微信推送（四选一）

个人微信没有开放的主动推送接口，任何「推送到微信」的方案都要经过一个中间通道。脚本已内置四种，**配哪个就用哪个**，都没配则只在日志里打印内容、不推送。

| 通道 | Secret 名 | 怎么拿 |
|---|---|---|
| **企业微信机器人**（推荐） | `WECHAT_WORK_WEBHOOK` | 企业微信群 → 群设置 → 群机器人 → 添加 → 复制 Webhook 地址 |
| **Server 酱 Turbo** | `SERVERCHAN_KEY` | 微信搜索「Server酱」公众号 → 添加后得到 SendKey |
| **PushPlus** | `PUSHPLUS_TOKEN` | pushplus.plus 注册后拿 Token |
| **WxPusher** | `WXPUSHER_APPTOKEN` + `WXPUSHER_UID` | wxpusher.zjiecode.com 扫码后获取 |

企业微信机器人最快，配置最简单（一个地址就够），如果公司微信在用优先选它。

设置方式（任选其一）：

```bash
# 网页粘贴（无需 gh）
# Settings → Secrets and variables → Actions → New repository secret
#   Name: WECHAT_WORK_WEBHOOK
#   Secret: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx

# 或用 gh CLI
echo -n "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx" | gh secret set WECHAT_WORK_WEBHOOK --repo <你的账号>/<仓库名>
```

Server 酱 / PushPlus / WxPusher 只需把上表里的 Secret 换成对应名称，值填它们的 key/token 即可，代码无需改动。

### 4. 手动触发验证

Actions 页签点 **Run workflow**，或在仓库 Settings → Actions 启用。

## 文件说明

| 文件 | 作用 |
|---|---|
| `signin.py` | 上游签到核心脚本（来自 88lin/workbuddy-auto-signin，MIT） |
| `scripts/refresh_secret.py` | 本机读登录态 → 注入 GitHub secret |
| `scripts/run_daily.py` | 云端入口，编排签到 → 猫猫 → 推送 |
| `scripts/push.py` | 微信推送（企业微信/Server酱/PushPlus/WxPusher 四通道），分区块 + 脱敏 |
| `.github/workflows/daily-signin.yml` | 定时任务定义 |

## 运行频率

| 任务 | cron (UTC) | 北京时间 | 作用 |
|---|---|---|---|
| 主任务 | `5 16 * * *` | 00:05 | 当天首次签到，始终推送 |
| 轮询 | `5 0,4,8,12,20 * * *` | 08:05 / 12:05 / 16:05 / 20:05 / 04:05 | 及时领旅行礼物、补签、补派名额 |

**为什么要轮询**：Buddy 出去旅行 1~4 小时就带礼物回来，礼物得手动领。只靠每天一次，礼物会压到第二天才到账；而且旅行**每日只有一个派出名额**，当天没派成就整个浪费掉。轮询每次都会先查签到状态，未签才补（接口幂等，已签只是查询，代价可忽略）。

**轮询不打扰你**：轮询时默认静默，只有这几种情况才推送——

- 签到失败或返回异常状态（需要你知道）
- 真的领到积分
- 领到旅行礼物 / 派出新一趟 / 开盲盒 / 连登奖励兑换

其余情况（今日已签到、旅行中、今天名额已用完）只留 Actions 日志，不打扰。

> 手动触发（Run workflow）始终推送，方便验证。
> 想调整频率直接改 `.github/workflows/daily-signin.yml` 里的 cron。

## 说明

- 定时为每天 16:05 UTC（北京 00:05）主任务 + 5 次轮询，GitHub cron 使用 UTC。共享 runner 在整点容易排队，故所有 cron 都避开 0 分（用 5 分）。
- `concurrency` 保证同一时间只有一个实例，避免重复领取。
- `X-Device-Token`：客户端签到请求会带 TuringShield 设备风控令牌，但实测服务端在成长中心/签到接口上未强制校验，该脚本不带也能正常领取（HTTP 200）。若后续服务端加强校验，需改为在本机生成该令牌并一并注入 secret。

## 踩坑记录

- **只在 WorkBuddy 桌面端存在**：该活动入口仅桌面客户端有，网页端无。
- **`code=10001` 是幂等成功**，不是失败。「今天已签到，请明天再来」属正常结果，应判成功，误判会导致每天误报失败。
- **沙箱内读取登录态会报 PermissionError**：某些受限环境下读`workbuddy-desktop.info` 被拦（表现为「请重新登录桌面端」，实际是权限问题而非登录态失效）。刷新脚本请在普通终端运行，不要在受限沙箱内执行。
- **`refresh_secret.py` 只处理明文凭据**：若客户端升级后改为 `sym-v1` 加密信封，脚本会明确报错而非产出不可用凭证。届时需在客户端内解密后再注入。
- **签到凭证明文、加密分两种**：你的客户端当前为明文（`doctor` 输出 `credential_format: plaintext`），故刷新脚本无需 Electron 运行时即可独立运行。

## 凭证泄露了怎么办

`WB_AUTH_FILE` 里是WorkBuddy 的登录 accessToken（你的环境是明文）。若不慎泄露：

1. 立刻打开 WorkBuddy 桌面端**退出登录**，让旧 token 失效。
2. 重新登录，客户端会生成新的登录态。
3. 重新执行 `refresh_secret.py --emit`，把新值粘贴覆盖到 secret。
4. 若曾在公开场合（issue、聊天、截图）泄露，**必须**走第1 步，仅覆盖 secret 不够——旧 token 在被吊销前一直有效。

GitHub侧也可在 Settings → Secrets 里删除该 secret，删除后 workflow 会因缺凭证而明确报错，不会静默跳过。

## 常见报错对照

| 现象 | 原因 | 处理 |
|---|---|---|
| `未配置 secret WB_AUTH_FILE` | secret 没建或名字拼错 | 重新按步骤 2 建secret |
| `AUTH_REJECTED` / HTTP 401 | token 过期或已失效 | 桌面端重新登录，再刷新 secret |
| `PermissionError` 读登录态失败 | 在受限沙箱内运行 | 换普通终端重跑 |
| `检测到加密凭据（sym-v1）` | 客户端升级为加密信封 | 需在客户端内解密，当前脚本不支持 |