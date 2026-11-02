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

**交互**：悬停展开，拖动移动位置（会记住），双击回到屏幕顶部正中。后台运行，不占任务栏、不抢焦点，周围区域鼠标可直接穿透。

**仪表盘**（`http://127.0.0.1:8765/`）：额度与花费、按天分组的完整派活记录（可展开看任务与结果）、主 agent 会话与 token 用量。

全部只读本地数据，不改动任何 agent 的文件。

## 安装

需要 Windows 10/11、Python 3.10+（或 [uv](https://docs.astral.sh/uv/)），以及 Edge 自带的 WebView2。

作为 Claude Code skill 安装（推荐，之后可以直接对 Claude 说"打开灵动岛""我还剩多少额度"）：

```powershell
git clone https://github.com/Lichenshuo/AutoSwarmagent-island "$HOME\.claude\skills\agent-island"
powershell -ExecutionPolicy Bypass -File "$HOME\.claude\skills\agent-island\install.ps1" -Autostart
```

`-Autostart` 表示登录 Windows 后自动启动，去掉即只安装；`install.ps1 -NoAutostart` 可取消自启。

之后双击 `island.cmd` 启动灵动岛，`dashboard.cmd` 同时打开仪表盘。

## 数据来源

| 内容 | 来源 |
|---|---|
| Claude 额度 | 终端里已登录的 Claude Code（`claude auth login`）→ 实时；否则读 Claude 桌面端本地的用量采样（不定期更新，会标注采样时间） |
| Codex 额度 | `~/.codex/sessions` 里最新的 `rate_limits` |
| Claude API 花费 | OpenCode 数据库中每条消息的 `cost`；默认 `~/.local/share/opencode/opencode.db`，可在 `config.json` 里追加（见 `config.example.json`），v1 / v2 自动识别 |
| Claude API 余额 | 有管理员密钥（`anthropic_admin_key`）时用官方 Cost API，精确；否则为"上次校准的余额 − 之后本地花费 × 修正系数"的估算 |
| 派活记录 | AutoSwarm 的调用记录（含 Claude 桌面端 MSIX 重定向目录），状态由 AutoSwarm 自己判定 |
| 主 agent 会话 | `~/.claude/projects` 下的会话记录（按消息 ID 去重） |

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

## License

MIT
