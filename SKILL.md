---
name: agent-island
description: Install, open or query Agent Island, a Windows Dynamic-Island style widget and local dashboard for multi-agent work (Claude Code, Codex, OpenCode, AutoSwarm). Shows remaining Claude / Codex subscription quota (5-hour and weekly), Claude API spend, running / waiting-for-approval / failed sub-agent tasks and delegation history. Use when the user asks to install or open the island / dashboard / 灵动岛 / 仪表盘, check remaining quota / 额度 / 花了多少钱, or review what sub-agents are doing / 派活记录.
---

# Agent Island

A black pill pinned to the top of the screen (hover to expand, drag to move, double-click to re-center) plus a full dashboard at `http://127.0.0.1:8765/`. Everything is local and read-only: it never writes to agent data, only to its own `data/` folder. Windows only.

`<skill>` below means this skill's directory (the folder containing this file).

## Install (once)

Needs Python 3.10+ (or `uv`) and the WebView2 runtime that ships with Microsoft Edge.

```
powershell -ExecutionPolicy Bypass -File "<skill>\install.ps1"            # add -Autostart to start at sign-in
```

Optional `<skill>\config.json` (copy `config.example.json`) lists extra OpenCode databases to count API spend from; by default `~/.local/share/opencode/opencode.db` is used. Schema v1/v2 is detected automatically.

## Open

- Island only: run `<skill>\island.cmd`
- Island + dashboard in the browser: run `<skill>\dashboard.cmd`

Both are safe to repeat: a second start only opens the browser if asked. The island runs in the background with no taskbar button or console; quit from the island's expanded view.

## Answer questions from its data

Fetch `http://127.0.0.1:8765/api/data` (start `island.cmd` first if it is not running) and summarize in the user's language:

- `claude.windows` / `codex.windows`: `used` is percent used, `resets_at` is Unix seconds or null. Remaining = 100 − used; a window past `resets_at` has reset. `captured_at` tells how fresh the reading is; a 5-hour reading older than 5 hours is meaningless. `claude.quota_source`: `oauth` (live, from the terminal Claude Code login), `desktop` (Claude Desktop's own samples) or `statusline`.
- `api.today | week | d30 | month`: Claude API spend in USD from OpenCode (`week` and `d30` are rolling 7 and 30 days, `month` is the calendar month); `api.sources` breaks it down per database.
- `calls`: AutoSwarm delegations, newest first. Skip `probe: true` (installation self-checks). `state` is `running`, `waiting` (blocked on an approval, see `approvals`), `completed`, `failed`, `lost` (runner died) or `cancelled`.
- `summary`: counts for today, running, waiting and failed.
- `calls[].usage`: `scope: "call"` means `tokens` / `cost` are what THIS round used (growth of the conversation's counter while it ran); `scope: "session"` means only the whole conversation's total was available. The same agent is reused for many rounds, so never read a call's session total as that call's cost.
- `delegation`: `totals` (agents, calls, completed, failed, running, tokens, cost, today, `by_provider`) and `agents[]` grouped per sub-agent (name, rounds, completed/failed, tokens, last state/task). Use it to answer "how many sub-agents / how many tokens have I dispatched".
- `codex.tokens_today` / `codex.tokens_week`: Codex tokens (cache included) from its session counters.

## Quota sources

- **Claude**: best is a signed-in terminal Claude Code (`claude auth login`); the island then reads live usage with reset times. Otherwise it falls back to Claude Desktop's local usage samples, which are only refreshed occasionally, and says how old they are.
  If the live reading stops (`claude.live_error`: `token_expired` = the terminal login token expired, `no_login`, `rate_limited`), the island falls back to the freshest of Claude Desktop's samples / the statusline snapshot and shows why. `server.py --quota-debug` prints every source side by side (no secrets) when the numbers look wrong. The island keeps the token alive by launching a short headless Claude Code process (no prompt, so no quota) when it has under 20 minutes left; Claude Code does the refresh with its own lock, the island only watches the expiry. `server.py --renew-token` runs this once and reports which method worked (`claude auth status`, then a headless session; the winner is remembered in `data/claude-renew.json`). If it fails, `claude.renew.ok` is false and the user must run `claude auth login`. A used-up weekly window means the 5-hour one is shown as blocked too.
- **Codex**: asked live every 3 minutes from `codex app-server` (`account/rateLimits/read`, Codex handles its own login); if that fails (`codex.live_error`) it falls back to the latest `rate_limits` in `~/.codex/sessions`, which only changes while Codex runs. Set `"codex_exe"` in `config.json` if `codex` is not on PATH.
- **API spend**: summed from per-message costs in the OpenCode databases.

## API balance

`api.billing` holds the balance. `source: "admin"` means exact numbers from Anthropic's Cost API (`anthropic_admin_key` in `config.json`). `source: "estimate"` means: last balance the user reported − local spend since then × `factor`. Local OpenCode costs undercount real billing (and miss other callers on the same key), so `factor` is learned automatically at each calibration (real spend ÷ local spend, smoothed). `stale: true` means the estimate is old or mostly extrapolated: say so and ask for a fresh number instead of presenting it as exact.

When the user tells you the balance the Console shows (e.g. "API 余额现在 5 美元，本月 128"), record it:

```
"<skill>\.venv\Scripts\python.exe" "<skill>\app\server.py" --calibrate 5.00 --month 128
```

`--month` (this month's spend) and `--total` (all credit ever topped up) are optional. It prints the learned factor; a running island picks it up within 20 s. The user can also do this themselves: click the API tile on the island, or "校准余额" on the dashboard's API card.
