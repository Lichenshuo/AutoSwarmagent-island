# Agent Island

把多 agent 协作的状态挂在 Windows 桌面顶端：一个苹果灵动岛风格的黑色胶囊，外加一个本地仪表盘。适合用 Claude Code 当主 agent、Codex / OpenCode 当副 agent（尤其配合 [AutoSwarm](https://github.com/HKUDS/AutoSwarm)）的工作流。

A Dynamic-Island style status pill for Windows plus a local dashboard, for people running Claude Code, Codex and OpenCode together (optionally through AutoSwarm).

## 能看到什么

**收起时**（始终在顶端）

- Claude、Codex 各一个**双半环**：左半环是 5 小时额度，右半环是本周额度，旁边写着剩余百分比；绿 / 橙 / 红表示宽松到紧张
- 正在进行的副 agent 数、**待批准**数、**异常**数（为 0 时不显示）
- 今日 Claude API 花费

**悬停展开**

- 两个订阅的 5 小时 / 本周剩余与重置倒计时，API 今日 / 近 7 天 / 近 30 天花费
- 进行中列表（运行时长实时走动，待批准会写明在等什么）
- 需要关注（失败、中断，可一键忽略；24 小时后自动收起）
- 最近完成
- 任务结束时胶囊会弹出 5 秒提示

**套餐自适应**：额度环按接口实际返回的窗口来画。比如 Codex Pro 没有 5 小时额度，就只显示一个周额度环和一行「本周」，不会留一个空的 5 小时。

**API 余额按厂商分开**：OpenCode 里用过哪些厂商（Claude、OpenAI / Codex、Gemini……）就各出一份，花费、余额估算和校准互相独立；岛上 Claude 是主格，其他厂商各占一行。Anthropic 和 OpenAI 都没有能读到预付费余额的公开接口，所以余额是「上次校准的余额 − 之后的花费」的估算（Claude 有管理员密钥时是精确的）。岛上不再常驻「点此校准」的提示，要校准时点 API 那一格，或在仪表盘对应厂商的卡片上点「校准余额」；命令行：`server.py --calibrate 19 --provider openai --total 60`。

**异常清理**：仪表盘「派活记录」里，「异常」按钮带待处理条数；每条异常右侧有「忽略」，上方有「清理全部异常」，清理后 12 秒内可「撤销」，「显示已忽略」可以找回并「恢复」。同一个 agent 后来又派过活的失败算「已重派」，不再算待处理。忽略只是不再显示，AutoSwarm 的记录不会被改动；灵动岛和仪表盘共用同一份忽略列表。

**交互**：悬停展开，拖动移动位置（会记住），双击回到屏幕顶部正中。后台运行，不占任务栏、不抢焦点，周围区域鼠标可直接穿透。

**仪表盘**（`http://127.0.0.1:8765/`）：额度与花费（Claude / Codex 都有今日、近 7 天 token）、**副 agent 总览**（派过多少个 agent、派活多少次、累计 token 和花费，以及每个 agent 跑了几轮、完成 / 异常、耗了多少 token，点一个 agent 只看它的记录）、按天分组的完整派活记录（可展开看任务与结果，每条显示的是**这一轮**用的 token，不是整个会话的累计）、主 agent 会话与 token 用量。

全部只读本地数据，不改动任何 agent 的文件。

## 安装

需要：
- **Windows 10 / 11**（灵动岛用到 Windows 窗口接口，macOS / Linux 只能单独跑仪表盘：`python app/server.py --open`）
- **Python 3.10+**（或 [uv](https://docs.astral.sh/uv/)，装了 uv 就不用另装 Python）
- **Edge WebView2 运行时**（Win 11 自带，Win 10 通常随 Edge 已有；没有的话安装脚本会提示）
- 可选：**Git**（用来装 AutoSwarm，才有派活记录；没有就只缺这一块）

没装的东西都会在安装脚本最后明确提示，装失败会直接停下来说原因，不会假装成功。

作为 Claude Code skill 安装（推荐，之后可以直接对 Claude 说"打开灵动岛""我还剩多少额度"）：

```powershell
git clone https://github.com/Lichenshuo/AutoSwarmagent-island "$HOME\.claude\skills\agent-island"
powershell -ExecutionPolicy Bypass -File "$HOME\.claude\skills\agent-island\install.ps1" -Autostart
```

**更新**：进入安装目录运行 `git pull`，然后退出灵动岛再重开。你的 `config.json`、`data` 和 `.venv` 不在版本库里，不会被覆盖。

`-Autostart` 表示登录 Windows 后自动启动，去掉即只安装；`install.ps1 -NoAutostart` 可取消自启。

之后双击 `island.cmd` 启动灵动岛，`dashboard.cmd` 同时打开仪表盘。

**第一次打开你会看到什么**：它只读你电脑上已有的东西，什么都没有就显示空状态，不会报错。
- 没装 / 没登录 Claude Code：Claude 额度显示"终端没登录"，在终端运行一次 `claude auth login` 即可
- 没用过 Codex：Codex 额度为空，用过一次就有
- 没装 AutoSwarm：没有派活记录和副 agent 总览，额度和 API 花费照常
- API 余额：第一次需要点灵动岛上的 API 一栏填一次 Console 里的余额（见下）

**出问题时**：灵动岛没有窗口、没有控制台，启动失败会弹窗说明，细节在 `data\island.log`。

## 数据来源

| 内容 | 来源 |
|---|---|
| Claude 额度 | 终端里已登录的 Claude Code（`claude auth login`）→ 实时，登录令牌快过期时灵动岛会自动拉起一个无界面的 Claude Code 去续期（不发消息，不耗额度，用 Claude Code 自己的续期和锁，不会挤掉终端登录）；否则读 Claude 桌面端本地的用量采样（不定期更新，会标注采样时间） |
| Codex 额度 | 每 3 分钟向 Codex 自己的 `codex app-server` 问一次当前额度（登录由 Codex 自己处理）；问不到时退回 `~/.codex/sessions` 里最新的 `rate_limits`，那只在 Codex 运行时才更新 |
| Claude API 花费 | OpenCode 数据库中每条消息的 `cost`；默认 `~/.local/share/opencode/opencode.db`，可在 `config.json` 里追加（见 `config.example.json`），v1 / v2 自动识别 |
| Claude API 余额 | 有管理员密钥（`anthropic_admin_key`）时用官方 Cost API，精确；否则为"上次校准的余额 − 之后本地花费 × 修正系数"的估算 |
| DSH（DeepSeek Harness）| 读 `~/.dsh/sessions` 下每个会话的 `session.v4.jsonl.zstd`（需要 `zstandard`，安装脚本会装）：每一轮的 token、模型、思考档位。DeepSeek 只有 API 计费：账号登录（`deepseek-account/…`）和 API 密钥的用量合并成一张 DeepSeek 卡 |
| DeepSeek 余额 | 有 API 密钥时调官方 `GET /user/balance`（密钥只发给 api.deepseek.com，来源：`config.json` 的 `deepseek_api_key`，或 DSH 配置里 `providers → deepseek → apiKeyEnv` 指定的环境变量，或 `DEEPSEEK_API_KEY`）。已使用金额：填过累计充值额就用「累计充值 − 余额」，否则按余额的下降累计 |
| DSH 里 Claude / OpenAI 等的金额 | DSH 只记 token 不记金额，没有公开余额接口，所以是估算：token × 你填的每百万 token 单价；填了充值总额还会显示估算剩余 |
| 派活记录 | AutoSwarm 的调用记录（含 Claude 桌面端 MSIX 重定向目录），状态由 AutoSwarm 自己判定 |
| 主 agent 会话 | `~/.claude/projects` 下的会话记录（按消息 ID 去重） |

## 第一次用：让读数更准

打开仪表盘，顶部如果有「让读数更准」，就是还有设置没做完，照着每一项的命令做即可；全部做完这一块会自动消失。灵动岛底部也会有橙色的「N 项可让读数更准」，点一下直接打开。

## DSH（DeepSeek Harness）额度

检测到 `~/.dsh`（或 `DSH_HOME`）才会出现，没有 DSH 的电脑完全不显示。DSH 接入了哪些厂商（读 DSH 配置里的 `providers`，比如 DeepSeek、Claude、OpenAI / Codex），就各出一份金额：仪表盘「API 额度」每个厂商一张卡，灵动岛上订阅额度（Claude、Codex）下面单独一行 API，每个厂商一格。DSH 里没接的厂商不显示。

- **DeepSeek**：要**给灵动岛单独建一把 API 密钥**才能读到精确余额（DSH 自己管理的那把读不出来）：DeepSeek 开放平台 → API keys → 创建 → 复制，再运行 `setx DEEPSEEK_API_KEY "密钥"` 并重启灵动岛。接上后圆环显示剩余百分比，旁边是剩余金额和已用金额；圆环的总数是「剩余 + 自开始记录起花掉的」，填过累计充值额就按真实总额算。没接时会在「让读数更准」里提醒。
- **账号登录和 API 密钥**：DeepSeek 只有 API 计费，两种登录方式的用量合并成一张卡；仪表盘和岛上的模型旁会标注这一轮是哪种登录。
- **Claude / OpenAI 等**：没有金额接口，按 token 估算，金额前带 ≈。内置了 Claude 和 DeepSeek 的公开标价作为参考（按每条消息实际用的模型算，DeepSeek 为美元标价），不用设置也能看到大概金额；想更准，一次性设置自己的单价（每百万 token，美元；缓存价可不填，不填按输入价算，会偏高）：

```powershell
python app\server.py --dsh-price anthropic 3 15 0.3 3.75 --currency usd
python app\server.py --dsh-credit anthropic 50        # 可选：充值总额，用来显示估算剩余
python app\server.py --deepseek-total CNY 100         # 可选：DeepSeek 累计充值（含赠送），已使用金额更准
```

仪表盘上每张卡下方都会给出带你本机真实路径的命令，点一下就能复制。

## API 余额校准

OpenCode 本地记录的花费通常比 Anthropic 实际账单低不少（长上下文、缓存计费，以及同一个 key 下其他程序的调用都看不到），所以没有管理员密钥时，余额只能估算：

1. 打开 [Console → Billing](https://console.anthropic.com/settings/billing)，看到当前余额。
2. 点灵动岛展开后的 **API 余额** 一栏（或仪表盘 API 卡片右下角的 **校准余额**），填入余额，保存。
3. 每次校准都会用"两次校准之间实际花掉的钱 ÷ 本地记录到的花费"更新修正系数（与旧系数加权平均，防止一次噪声带偏），后面的估算会越来越准。余额变多时按充值处理，自动加到累计充值总额。

估算超过 6 小时、或估算出的花费已超过上次余额的 30%，灵动岛上会出现橙色 **?** 提醒重新校准。也可以直接对 Claude 说"API 余额现在 xx"，或命令行：

```powershell
& "$HOME\.claude\skills\agent-island\.venv\Scripts\python.exe" "$HOME\.claude\skills\agent-island\app\server.py" --calibrate 5.00 --month 128
```

## 结构

```
SKILL.md            给 agent 看的说明
install.ps1         安装 / 自启开关
island.cmd          启动灵动岛
dashboard.cmd       启动并打开仪表盘
app/server.py       取数与本地 HTTP 服务
app/island.py       灵动岛窗口（pywebview + Win32）
app/island.html     灵动岛界面
app/index.html      仪表盘界面
app/statusline.py   可选：Claude Code 终端状态栏，顺带记录额度
```

界面遵循 Apple 设计语言，设计规范参考 [apple-design-skill](https://github.com/naplesblue/apple-design-skill)。

## 更新日志

每次发布都会在这里记一条，并打同名标签（`git tag`）。已装过的人 `git pull` 即可更新，更新后退出灵动岛再重开。

### 0.005（开发中）
- 新增：仪表盘顶部「架构实时图」，终端风格——主 agent → 派发 → 三路副 agent → 回到主 agent 验证，下方滚动最近 6 条动作和当前正在做的事。每个 agent 一行：名字、角色标签、用的模型（DSH 里会标明是 deepseek 还是 claude 等 API，以及账号登录 / API 密钥）、正在干什么
- 新增：读取 Claude 自己派的内部子 agent（`~/.claude/projects/…/subagents/agent-*.jsonl`），显示它的角色（Explore 等）、模型、token 和正在做的步骤；最近 2 分钟有动静算运行中
- 有任务在跑时连线上有流动的光点，没任务时动画自动停止；纯 HTML + CSS，没有额外依赖，内容没变化就不重绘

### 0.004
- 修复：DeepSeek 充值后圆环和「已使用」不更新——余额变高时自动把增加的部分记入累计充值额，余额缓存缩短到 1 分钟，读取失败不再缓存
- 修复：填过一次累计充值总额后，仪表盘上再也找不到修改入口——现在一直显示「手动改累计充值额」

### 0.003
- 新增 DSH（DeepSeek Harness）支持：按 DSH 接入的厂商（DeepSeek / Claude / OpenAI 等）在仪表盘「API 额度」和灵动岛的 API 一行里分别显示金额；派活记录有 DSH 筛选，每轮显示 token、模型、思考档位，并标注登录方式（账号登录 / API 密钥，DeepSeek 用量合并计算）
- DeepSeek 用官方余额接口读账户余额和已使用金额；Claude / OpenAI 等经 DSH 的按 token 估算（一次性填单价，不用定期校准）
- 有 DSH 时，原来基于 OpenCode 的 Claude API 卡片不再显示（API 额度统一走 DSH）
- 仪表盘里可以直接点「填单价」弹窗设置，不用命令行
- DSH 派活记录的模型和思考档位，按这一次派活时 DSH 实际运行的配置显示（不再用整个会话最后一次的设置）；`server.py --dsh-calls` 可对比 AutoSwarm 记录和会话里的实际值
- 灵动岛展开后圆环排成一行、对齐：DeepSeek 有真实余额时画成圆环（剩余百分比、剩余金额、已用金额）；没有余额接口的第三方厂商（Claude、OpenAI 等）不画圆环，只显示已用金额的小标签
- 新增「让读数更准」清单：仪表盘顶部列出还能做的设置（Claude 终端登录、Codex 网络、DSH 解压组件、DeepSeek API 密钥……），每项附带带你本机路径的现成命令；灵动岛底部有提示，点一下打开
- 新增依赖 `zstandard`（读 DSH 会话），装不上只缺 DSH 的 token 统计

### 0.002
- Claude 额度：只登录桌面端、没登录终端的电脑也能读。自动在 `AppData` 里找桌面端的用量文件（常见位置 + 一次限时扫描），找不到时仪表盘会给出一条 `--find-desktop` 命令，运行后自动记住位置
- 只有桌面端数据时，提示改为"数据来自桌面端，可能不是最新"，不再像报错
- 仪表盘「副 agent 总览」：正在运行的放在最上面，空闲的收进"空闲的 N 个"折叠条
- 灵动岛每行显示思考程度（思考 effort）
- 只有每周额度的套餐（如 Codex Pro）只显示周额度；修了额度格子文字被遮挡
- OpenCode 的 API 花费按厂商区分，各自校准；不再一直提示校准
- 后台服务还是旧版本时，灵动岛和仪表盘会提示"请退出后重开"
- 修复：窗口启动方式恢复为默认，避免个别电脑上灵动岛不出现

### 0.001
- 更安全的安装流程，缺依赖时给出明确提示

## License

MIT
