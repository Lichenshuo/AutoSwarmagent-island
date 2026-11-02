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

## Quota sources

- **Claude**: best is a signed-in terminal Claude Code (`claude auth login`); the island then reads live usage with reset times. Otherwise it falls back to Claude Desktop's local usage samples, which are only refreshed occasionally, and says how old they are.
- **Codex**: read from the latest `rate_limits` in `~/.codex/sessions`, so it updates whenever Codex runs.
- **API spend**: summed from per-message costs in the OpenCode databases.
