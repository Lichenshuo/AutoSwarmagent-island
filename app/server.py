"""Local dashboard for AutoSwarm: quotas, API spend and delegation history.

Read-only: it never writes to agent data, only to its own data/ directory.
"""
import argparse
import copy
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:  # AutoSwarm is optional: without it the delegation history is simply empty.
    from autoswarm import engine as swarm_engine
    from autoswarm.records import Records
except ImportError:
    swarm_engine = Records = None

HOME = Path.home()
ROOT = Path(__file__).resolve().parent
DATA = ROOT.parent / "data"
CONFIG_FILE = ROOT.parent / "config.json"
LOCAL = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData/Local"))
# Claude Desktop is an MSIX app: part of what its child processes write under
# AppData is redirected into the package's LocalCache, so read both views.
CLAUDE_PACKAGE = next(iter(sorted((LOCAL / "Packages").glob("Claude_*"))), None) if (LOCAL / "Packages").exists() else None
AUTOSWARM_HOMES = [Path(os.environ.get("AUTOSWARM_HOME", LOCAL / "autoswarm"))]
if CLAUDE_PACKAGE:
    AUTOSWARM_HOMES.append(CLAUDE_PACKAGE / "LocalCache" / "Local" / "autoswarm")
CODEX_SESSIONS = HOME / ".codex" / "sessions"
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CLAUDE_ACCOUNT = HOME / ".claude.json"
CLAUDE_DESKTOP_DATA = next(iter(sorted((LOCAL / "Claude-Data").glob("claude_*"))), None) if (LOCAL / "Claude-Data").exists() else None
CLAUDE_STATUS = DATA / "claude-status.json"
CLAUDE_WINDOW_DAYS = 7


def opencode_dbs():
    """OpenCode databases to read API spend from: config.json "opencode_dbs" or the default location."""
    config = read_json(CONFIG_FILE) or {}
    entries = config.get("opencode_dbs") or [{"label": "OpenCode", "path": str(HOME / ".local/share/opencode/opencode.db")}]
    return [(e.get("label") or "OpenCode", Path(os.path.expanduser(e["path"]))) for e in entries if e.get("path")]


def opencode_flavor(con):
    """OpenCode v2 keeps messages in session_message, v1 in message."""
    tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
    return "v2" if "session_message" in tables else "v1" if "message" in tables else None


def local_day(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def iso_to_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_json(path):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return None


# ── Codex ────────────────────────────────────────────────────────────────

_codex_cache = {}


def gain(events, start, end, idx=1):
    """How much a cumulative counter grew between start and end (unix seconds).

    events: [(ts, cumulative_a, cumulative_b, ...)] sorted by ts; idx picks which counter. None when unknown."""
    if not events:
        return None
    base = last = 0
    for ev in events:
        if ev[0] <= start:
            base = ev[idx]
        if ev[0] <= end + 3:       # a few seconds of slack: the log is flushed just after the call returns
            last = ev[idx]
        else:
            break
    return max(0, last - base)


def counter_days(events, idx=1):
    """Per local day increase of a cumulative counter."""
    out, prev = {}, 0
    for ev in events or []:
        cur = ev[idx]
        step = cur - prev if cur >= prev else cur      # a drop means the counter restarted
        prev = cur
        if step > 0:
            day = local_day(ev[0])
            out[day] = out.get(day, 0) + step
    return out


def parse_codex_file(path):
    info = {"id": None, "cwd": None, "originator": None, "tokens": None, "rate": None, "rate_ts": 0, "events": []}
    with open(path, "rb") as fh:
        for raw in fh:
            if b'"session_meta"' in raw and info["id"] is None:
                try:
                    p = json.loads(raw)["payload"]
                    info.update(id=p.get("id"), cwd=p.get("cwd"), originator=p.get("originator"))
                except Exception:
                    pass
            elif b'"token_count"' in raw:
                try:
                    row = json.loads(raw)
                    p = row["payload"]
                except Exception:
                    continue
                usage = (p.get("info") or {}).get("total_token_usage")
                if usage:
                    info["tokens"] = usage
                    ts, total = iso_to_ts(row.get("timestamp")), usage.get("total_tokens")
                    if ts and isinstance(total, (int, float)) and (not info["events"] or info["events"][-1][1] != total):
                        info["events"].append((ts, int(total)))
                rl = p.get("rate_limits")
                # Codex also logs other limit families (e.g. "premium") with empty windows; keep the real ones.
                if rl and (rl.get("primary") or rl.get("secondary")):
                    info["rate"] = rl
                    info["rate_ts"] = iso_to_ts(row.get("timestamp")) or 0
    return info


def codex_snapshot():
    sessions, latest = {}, None
    if CODEX_SESSIONS.exists():
        cutoff = time.time() - 30 * 86400
        for path in CODEX_SESSIONS.rglob("*.jsonl"):
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            key = (st.st_mtime, st.st_size)
            cached = _codex_cache.get(path)
            if not cached or cached[0] != key:
                cached = (key, parse_codex_file(path))
                _codex_cache[path] = cached
            info = cached[1]
            if info["id"]:
                sessions[info["id"]] = {**info, "path": path}
            if info["rate"] and (latest is None or info["rate_ts"] > latest["rate_ts"]):
                latest = info
    limits = None
    if latest:
        rl = latest["rate"]
        limits = {
            "plan": rl.get("plan_type"),
            "captured_at": latest["rate_ts"],
            "windows": [
                {"key": key, "used": w.get("used_percent", w.get("used_percentage")), "window_minutes": w.get("window_minutes"), "resets_at": w.get("resets_at")}
                for key in ("primary", "secondary") if (w := rl.get(key))
            ],
            "reached": rl.get("rate_limit_reached_type"),
        }
    return sessions, limits


# ── Codex live quota, asked from Codex itself ────────────────────────────
# Session files only change while Codex runs, so the numbers go stale. `codex app-server` is Codex's own
# JSON-RPC-over-stdio interface; it handles its own login and answers account/rateLimits/read.

CODEX_LIVE_EVERY = 180
_codex_live = {"at": 0.0, "busy": False, "info": None, "error": None, "ok_at": None}
_codex_live_lock = threading.Lock()


def codex_exe():
    config = read_json(CONFIG_FILE) or {}
    for cand in (config.get("codex_exe"), shutil.which("codex"), LOCAL / "Programs/OpenAI/Codex/bin/codex.exe",
                 HOME / ".codex/bin/codex.exe", HOME / ".codex/bin/codex"):
        if cand and Path(cand).exists():
            return str(cand)
    return None


def _num(*values):
    for v in values:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _ts(v):
    if isinstance(v, (int, float)):
        return float(v) / 1000 if v > 1e12 else float(v)
    return iso_to_ts(v) if isinstance(v, str) else None


def _codex_windows(snap):
    windows = []
    for key in ("primary", "secondary"):
        w = snap.get(key)
        if not isinstance(w, dict):
            continue
        used = _num(w.get("usedPercent"), w.get("used_percent"), w.get("usedPercentage"), w.get("used_percentage"))
        if used is None:     # e.g. a family with empty windows
            continue
        minutes = _num(w.get("windowDurationMins"), w.get("window_minutes"), w.get("windowMinutes"))
        if minutes is None and _num(w.get("limit_window_seconds"), w.get("windowSeconds")):
            minutes = _num(w.get("limit_window_seconds"), w.get("windowSeconds")) / 60
        windows.append({"key": key, "used": used, "window_minutes": int(minutes) if minutes else None,
                        "resets_at": _ts(w.get("resetsAt") if w.get("resetsAt") is not None else w.get("resets_at"))})
    return windows


def parse_codex_rate_limits(result):
    """Normalise an account/rateLimits/read result. Field spellings differ across Codex versions and plans
    (some plans have no 5-hour window at all), so every candidate snapshot is tried and the first with a
    real window wins."""
    if not isinstance(result, dict):
        return None
    by_id = result.get("rateLimitsByLimitId") or result.get("rate_limits_by_limit_id")
    cands = []
    if isinstance(by_id, dict) and isinstance(by_id.get("codex"), dict):
        cands.append(by_id["codex"])
    cands += [result[k] for k in ("rateLimits", "rate_limits") if isinstance(result.get(k), dict)]
    if isinstance(by_id, dict):
        cands += [v for k, v in by_id.items() if k != "codex" and isinstance(v, dict)]
    cands.append(result)
    plan = next((c.get(k) for c in cands for k in ("planType", "plan_type") if isinstance(c.get(k), str)), None)
    for snap in cands:
        windows = _codex_windows(snap)
        if windows:
            return {"plan": plan, "windows": windows,
                    "reached": snap.get("rateLimitReachedType") or snap.get("rate_limit_reached_type")}
    return None


def _shape(v, depth=0):
    """The structure of a response with every string replaced (ids, emails), numbers and flags kept."""
    if isinstance(v, dict):
        return {k: _shape(x, depth + 1) for k, x in list(v.items())[:24]} if depth < 5 else "{...}"
    if isinstance(v, list):
        return [_shape(x, depth + 1) for x in v[:3]]
    if isinstance(v, str):
        return v if (len(v) <= 12 and v.replace("_", "").replace("-", "").isalnum()) else "<str>"
    return v


def _codex_fetch(debug=None):
    exe = codex_exe()
    if not exe:
        return None, "codex_missing"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            cwd=str(HOME), creationflags=flags)
    timer = threading.Timer(25, proc.kill)
    timer.start()
    try:
        send = [{"method": "initialize", "id": 1, "params": {"clientInfo": {"name": "agent-island", "title": "Agent Island", "version": "1.0"}}},
                {"method": "initialized"},
                {"method": "account/rateLimits/read", "id": 2}]
        proc.stdin.write(("\n".join(json.dumps(m) for m in send) + "\n").encode())
        proc.stdin.flush()
        for raw in proc.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("id") != 2:
                continue
            if debug is not None:
                debug["raw_keys"] = sorted((msg.get("result") or {}).keys()) if isinstance(msg.get("result"), dict) else None
                debug["shape"] = _shape(msg.get("result"))
            if msg.get("error"):
                return None, "codex_rpc: " + str((msg["error"] or {}).get("message") if isinstance(msg["error"], dict) else msg["error"])[:80]
            info = parse_codex_rate_limits(msg.get("result"))
            return (info, None) if info else (None, "codex_no_windows")
        return None, "codex_no_answer"
    finally:
        timer.cancel()
        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.kill()


def _codex_live_refresh():
    detail = {}
    try:
        info, error = _codex_fetch(detail)
    except Exception as exc:
        info, error = None, "codex_failed: " + str(exc)[:60]
    with _codex_live_lock:
        _codex_live.update(busy=False, error=error, detail=detail.get("shape") if error else None)
        if info:
            _codex_live.update(info=info, ok_at=time.time())


def codex_with_live(limits):
    """Merge Codex's own live reading (refreshed in the background, never blocks) over the session-file one."""
    with _codex_live_lock:
        if not _codex_live["busy"] and time.time() - _codex_live["at"] > CODEX_LIVE_EVERY:
            _codex_live.update(at=time.time(), busy=True)
            threading.Thread(target=_codex_live_refresh, daemon=True).start()
        live, ok_at, error = _codex_live["info"], _codex_live["ok_at"], _codex_live["error"]
        detail = _codex_live.get("detail")
    out = dict(limits) if limits else {"plan": None, "captured_at": None, "windows": [], "reached": None}
    if live and ok_at and time.time() - ok_at < 3600 and ok_at >= (out.get("captured_at") or 0):
        out.update(plan=live.get("plan") or out.get("plan"), captured_at=ok_at, windows=live["windows"],
                   reached=live.get("reached"), source="live")
        error = None
    else:
        out["source"] = "sessions"
    out["live_error"] = error
    out["live_error_detail"] = json.dumps(detail, ensure_ascii=False)[:600] if error and detail else None
    return out


# ── Claude Code (main agent) ─────────────────────────────────────────────

_claude_lock = threading.Lock()
_claude_state = {"files": {}, "ready": False, "scanning": False}
_claude_seen = set()  # resumed sessions copy history into new files; count each message once
CLAUDE_CACHE = DATA / "claude-cache.json"


def _claude_load_cache():
    cached = read_json(CLAUDE_CACHE)
    if cached and isinstance(cached.get("files"), dict):
        _claude_state["files"] = cached["files"]
        _claude_seen.update(cached.get("seen", []))


def _claude_save_cache():
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = CLAUDE_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"files": _claude_state["files"], "seen": list(_claude_seen)}), encoding="utf-8")
    tmp.replace(CLAUDE_CACHE)


def _claude_scan_file(path, entry):
    size = path.stat().st_size
    if size < entry.get("offset", 0):
        entry.clear()
    entry.setdefault("offset", 0)
    entry.setdefault("tot", {"in": 0, "out": 0, "cr": 0, "cw": 0, "n": 0})
    entry.setdefault("days", {})
    if size == entry["offset"]:
        return False
    with open(path, "rb") as fh:
        fh.seek(entry["offset"])
        for raw in fh:
            if not raw.endswith(b"\n"):
                break
            entry["offset"] += len(raw)
            _claude_line(raw, entry)
    return True


def _claude_line(raw, entry):
    is_usage = b'"usage"' in raw and b'"assistant"' in raw
    wants_title = "title" not in entry and b'"type":"user"' in raw
    if not (is_usage or wants_title or b'-title"' in raw):
        return
    try:
        row = json.loads(raw)
    except Exception:
        return
    kind = row.get("type")
    if kind in ("custom-title", "ai-title"):
        text = row.get("customTitle") or row.get("aiTitle")
        if isinstance(text, str) and text.strip() and (kind == "custom-title" or entry.get("named") != "custom"):
            entry["title"] = text.strip()[:80]
            entry["named"] = "custom" if kind == "custom-title" else "ai"
        return
    if row.get("cwd"):
        entry["cwd"] = row["cwd"]
    if row.get("sessionId"):
        entry["session"] = row["sessionId"]
    if kind == "user" and "title" not in entry and not row.get("isSidechain"):
        content = (row.get("message") or {}).get("content")
        if isinstance(content, list):
            content = next((c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"), None)
        if (isinstance(content, str) and content.strip() and not content.lstrip().startswith("<")
                and not content.startswith("Another Claude session")):
            entry["title"] = " ".join(content.split())[:80]
        return
    if kind != "assistant":
        return
    msg = row.get("message") or {}
    usage = msg.get("usage")
    mid = msg.get("id")
    if not usage or (mid and mid in _claude_seen):
        return
    if mid:
        _claude_seen.add(mid)
    values = {
        "in": usage.get("input_tokens") or 0,
        "out": usage.get("output_tokens") or 0,
        "cr": usage.get("cache_read_input_tokens") or 0,
        "cw": usage.get("cache_creation_input_tokens") or 0,
    }
    for k, v in values.items():
        entry["tot"][k] += v
    entry["tot"]["n"] += 1
    ts = iso_to_ts(row.get("timestamp")) or time.time()
    day = local_day(ts)
    entry["days"][day] = entry["days"].get(day, 0) + sum(values.values())
    entry["last_ts"] = max(entry.get("last_ts", 0), ts)
    if msg.get("model"):
        entry["model"] = msg["model"]


def claude_scan():
    if not CLAUDE_PROJECTS.exists():
        _claude_state["ready"] = True
        return
    with _claude_lock:
        if _claude_state["scanning"]:
            return
        _claude_state["scanning"] = True
    try:
        cutoff = time.time() - CLAUDE_WINDOW_DAYS * 86400
        changed = False
        files = _claude_state["files"]
        for path in CLAUDE_PROJECTS.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    continue
                # Scan a private copy, then publish it, so readers never see a half-updated entry.
                entry = copy.deepcopy(files.get(str(path), {}))
                if _claude_scan_file(path, entry):
                    with _claude_lock:
                        files[str(path)] = entry
                    changed = True
            except OSError:
                continue
        horizon = local_day(time.time() - 35 * 86400)
        with _claude_lock:
            for key in [k for k, v in files.items() if v.get("last_ts", 0) and local_day(v["last_ts"]) < horizon]:
                files.pop(key, None)
                changed = True
        if changed:
            _claude_save_cache()
    finally:
        _claude_state["ready"] = True
        _claude_state["scanning"] = False


CLAUDE_CREDENTIALS = HOME / ".claude" / ".credentials.json"


def _oauth_usage():
    """Live usage from the endpoint behind Claude Code's /usage, using the terminal login's token (read-only)."""
    creds = (read_json(CLAUDE_CREDENTIALS) or {}).get("claudeAiOauth") or {}
    token = creds.get("accessToken")
    if not token:
        return {"error": "no_login"}
    expires = creds.get("expiresAt")
    if isinstance(expires, (int, float)) and expires / (1000 if expires > 1e12 else 1) < time.time() + 30:
        # Only a running terminal Claude Code renews this token; refreshing it here could log the terminal out.
        return {"error": "token_expired"}
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json", "User-Agent": "agent-island"})
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {"error": {401: "token_expired", 429: "rate_limited"}.get(exc.code, f"http_{exc.code}")}
    except Exception as exc:
        return {"error": "network: " + str(exc)[:80]}
    windows = []
    for key in ("five_hour", "seven_day"):
        w = data.get(key)
        if isinstance(w, dict) and w.get("utilization") is not None:
            windows.append({"key": key, "used": float(w["utilization"]), "resets_at": iso_to_ts(w.get("resets_at"))})
    return {"captured_at": time.time(), "windows": windows, "source": "oauth"}


OAUTH_CACHE = DATA / "claude-oauth.json"
_oauth_lock = threading.Lock()
_oauth_next = [0.0]  # earliest time for the next request


_oauth_error = {"error": None, "at": 0.0}

# ── Renewing the terminal login through Claude Code itself ───────────────
# The usage endpoint needs the OAuth token in ~/.claude/.credentials.json, which only a running
# Claude Code renews. When it is about to expire we start a short-lived headless Claude Code
# process (no prompt is sent, so it costs no quota); it refreshes the token with its own
# cross-process lock, so the terminal's login is never fought over. We only watch the file's
# expiry to see whether it worked, and remember which method did.

RENEW_FILE = DATA / "claude-renew.json"
RENEW_AHEAD = 20 * 60          # renew when the token has less than this left
RENEW_RETRY = 10 * 60          # ...at most this often
_renew_lock = threading.Lock()
_renew_state = {"at": 0.0, "ok": None, "method": None, "error": None}


def claude_exe():
    config = read_json(CONFIG_FILE) or {}
    for cand in (config.get("claude_exe"), shutil.which("claude"), HOME / ".local/bin/claude.exe", HOME / ".local/bin/claude"):
        if cand and Path(cand).exists():
            return str(cand)
    return None


def token_expiry():
    exp = ((read_json(CLAUDE_CREDENTIALS) or {}).get("claudeAiOauth") or {}).get("expiresAt")
    if not isinstance(exp, (int, float)):
        return None
    return exp / 1000 if exp > 1e12 else float(exp)


def _clean_env():
    # Claude Code refuses some modes when it thinks it runs inside another Claude Code session.
    return {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")}


def _wait_expiry_moves(before, timeout, proc=None):
    end = time.time() + timeout
    while time.time() < end:
        exp = token_expiry()
        if exp and (before is None or exp > before + 60):
            return exp
        if proc is not None and proc.poll() is not None:
            exp = token_expiry()
            return exp if exp and (before is None or exp > before + 60) else None
        time.sleep(0.5)
    return None


def renew_claude_token(verbose=False):
    """Ask Claude Code to renew the login token. Returns {"ok", "method", "before", "after", "tried": [...]}."""
    if not _renew_lock.acquire(blocking=False):
        return {"ok": None, "error": "already running"}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    tried, result = [], None
    try:
        exe = claude_exe()
        before = token_expiry()
        if not exe:
            result = {"ok": False, "error": "no_claude_cli", "before": before, "tried": tried}
            return result
        methods = ["status", "headless"]
        remembered = (read_json(RENEW_FILE) or {}).get("method")
        if remembered in methods:
            methods.remove(remembered)
            methods.insert(0, remembered)
        for method in methods:
            started = time.time()
            try:
                if method == "status":
                    subprocess.run([exe, "auth", "status"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, cwd=str(HOME), env=_clean_env(), timeout=25, creationflags=flags)
                    after = _wait_expiry_moves(before, 3)
                else:
                    proc = subprocess.Popen([exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"],
                                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            cwd=str(HOME), env=_clean_env(), creationflags=flags)
                    try:
                        proc.stdin.write((json.dumps({"type": "control_request", "request_id": "island-init",
                                                      "request": {"subtype": "initialize"}}) + "\n").encode())
                        proc.stdin.flush()
                        after = _wait_expiry_moves(before, 30, proc)
                    finally:
                        try:
                            proc.stdin.close()
                        except OSError:
                            pass
                        proc.kill()
                tried.append({"method": method, "moved": bool(after), "seconds": round(time.time() - started, 1)})
            except Exception as exc:
                tried.append({"method": method, "moved": False, "error": str(exc)[:80]})
                after = None
            if after:
                DATA.mkdir(parents=True, exist_ok=True)
                RENEW_FILE.write_text(json.dumps({"method": method, "at": time.time()}), encoding="utf-8")
                result = {"ok": True, "method": method, "before": before, "after": after, "tried": tried}
                return result
            if before is not None and before > time.time() + 60:   # token was fine all along; nothing to renew
                result = {"ok": True, "method": None, "before": before, "after": before, "tried": tried, "note": "token still valid"}
                return result
        result = {"ok": False, "error": "not_renewed", "before": before, "tried": tried}
        return result
    finally:
        if result is not None:
            # "token still valid" = Claude Code decided it is not due yet: look again soon, not after the full retry gap.
            soon = result.get("note") == "token still valid"
            _renew_state.update(at=time.time() - (RENEW_RETRY - 180 if soon else 0),
                                ok=result.get("ok"), method=result.get("method"), error=result.get("error"))
        _renew_lock.release()


def maybe_renew():
    exp = token_expiry()
    if exp is None and not CLAUDE_CREDENTIALS.exists():
        return
    if exp is not None and exp - time.time() > RENEW_AHEAD:
        return
    if time.time() - _renew_state["at"] < RENEW_RETRY:
        return
    _renew_state["at"] = time.time()   # claim the slot
    renew_claude_token()


def _refresh_oauth():
    try:
        maybe_renew()
    except Exception:
        pass
    result = _oauth_usage()
    with _oauth_lock:
        if result and result.get("windows"):
            DATA.mkdir(parents=True, exist_ok=True)
            OAUTH_CACHE.write_text(json.dumps(result), encoding="utf-8")
            _oauth_next[0] = time.time() + 120
            _oauth_error.update(error=None, at=time.time())
        else:
            err = (result or {}).get("error") or "empty"
            _oauth_error.update(error=err, at=time.time())
            _oauth_next[0] = time.time() + (600 if err == "rate_limited" else 300 if err == "no_login" else 120)


def claude_live():
    """Live usage, polled politely in the background: every 2 min, 10 min back-off after a 429.
    Always returns the last good value immediately (kept on disk across restarts)."""
    with _oauth_lock:
        last = read_json(OAUTH_CACHE)
        now = time.time()
        if not _oauth_next[0] and last:  # fresh start: don't re-poll a value that is still recent
            _oauth_next[0] = (last.get("captured_at") or 0) + 120
        try:
            relogin = _oauth_error["error"] and CLAUDE_CREDENTIALS.stat().st_mtime > _oauth_error["at"]
        except OSError:
            relogin = False
        if now >= _oauth_next[0] or relogin:
            _oauth_error["at"] = now   # consume the "login changed" signal
            _oauth_next[0] = now + 30  # in flight; the worker sets the real next time
            threading.Thread(target=_refresh_oauth, daemon=True).start()
    return last


def claude_quota():
    """Best Claude subscription usage: live API (terminal login) > Desktop samples / statusLine snapshot."""
    live = claude_live()
    candidates = []
    if live and live.get("windows") and time.time() - (live.get("captured_at") or 0) < 86400:
        candidates.append(live)
    if CLAUDE_DESKTOP_DATA:
        history = read_json(CLAUDE_DESKTOP_DATA / "plan-usage-history.json") or {}
        samples = history.get("samples") or []
        if samples:
            last = max(samples, key=lambda s: s.get("t", 0))
            u = last.get("u") or {}
            windows = [{"key": key, "used": u.get(short), "resets_at": None}
                       for key, short in (("five_hour", "fh"), ("seven_day", "sd")) if u.get(short) is not None]
            candidates.append({"captured_at": last.get("t", 0) / 1000, "windows": windows, "source": "desktop"})
    status = read_json(CLAUDE_STATUS)
    if status:
        windows = [{"key": key, "used": w.get("used_percentage"), "resets_at": w.get("resets_at")}
                   for key in ("five_hour", "seven_day") if (w := (status.get("rate_limits") or {}).get(key))]
        candidates.append({"captured_at": status.get("captured_at") or 0, "windows": windows, "source": "statusline"})
    candidates = [c for c in candidates if c["windows"]]
    best = max(candidates, key=lambda c: c["captured_at"] or 0) if candidates else {"captured_at": None, "windows": [], "source": None}
    best = dict(best)
    # Tell the UI why the live reading is not being used (expired login, rate limit, ...).
    best["live_error"] = _oauth_error["error"] if best.get("source") != "oauth" or _oauth_error["error"] else None
    best["renew"] = {"ok": _renew_state["ok"], "error": _renew_state["error"], "at": _renew_state["at"] or None}
    return best


def claude_plan():
    account = (read_json(CLAUDE_ACCOUNT) or {}).get("oauthAccount") or {}
    tier = account.get("organizationRateLimitTier") or ""
    org = account.get("organizationType") or ""
    if "max" in org or "max" in tier:
        multiple = next((part for part in tier.split("_") if part.endswith("x") and part[:-1].isdigit()), None)
        return "max" + (" " + multiple if multiple else "")
    return org.replace("claude_", "") or None


def claude_snapshot():
    with _claude_lock:
        files = list(_claude_state["files"].values())
    today = local_day(time.time())
    week = [local_day(time.time() - i * 86400) for i in range(7)]
    sessions = {}
    for f in files:
        sid = f.get("session")
        if not sid or not f.get("tot", {}).get("n"):
            continue
        s = sessions.setdefault(sid, {"session": sid, "title": None, "cwd": f.get("cwd"), "model": None, "last_ts": 0,
                                      "tokens": 0, "today": 0, "messages": 0})
        if f.get("named") or not s["title"]:
            s["title"] = f.get("title") or s["title"]
        s["model"] = f.get("model") or s["model"]
        s["last_ts"] = max(s["last_ts"], f.get("last_ts", 0))
        t = f["tot"]
        s["tokens"] += t["in"] + t["out"] + t["cr"] + t["cw"]
        s["today"] += f["days"].get(today, 0)
        s["messages"] += t["n"]
    quota = claude_quota()
    return {
        "plan": claude_plan(),
        "captured_at": quota["captured_at"],
        "quota_source": quota["source"],
        "live_error": quota.get("live_error"),
        "renew": quota.get("renew"),
        "windows": quota["windows"],
        "tokens_today": sum(f.get("days", {}).get(today, 0) for f in files),
        "tokens_week": sum(f.get("days", {}).get(d, 0) for f in files for d in week),
        "ready": _claude_state["ready"],
        "sessions": sorted(sessions.values(), key=lambda s: s["last_ts"], reverse=True)[:12],
    }


def quota_debug():
    """Print every source of the Claude 5h / weekly numbers side by side. Never prints tokens."""
    now = time.time()
    iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%m-%d %H:%M:%S") if ts else None
    creds_raw = read_json(CLAUDE_CREDENTIALS) or {}
    oauth = creds_raw.get("claudeAiOauth") or {}
    exp = oauth.get("expiresAt")
    exp = exp / 1000 if isinstance(exp, (int, float)) and exp > 1e12 else exp
    out = {"now": iso(now),
           "login_file": {"path": str(CLAUDE_CREDENTIALS), "exists": CLAUDE_CREDENTIALS.exists(),
                          "has_token": bool(oauth.get("accessToken")), "token_expires": iso(exp),
                          "token_expired": bool(exp and exp < now), "plan": oauth.get("subscriptionType")}}
    live = _oauth_usage()
    if live and live.get("windows"):
        out["live_api"] = {w["key"]: {"used_pct": w["used"], "resets_at": iso(w["resets_at"])} for w in live["windows"]}
        out["live_api_keys"] = live.get("keys")
    else:
        out["live_api"] = live
    cache = read_json(OAUTH_CACHE) or {}
    out["cache_file"] = {"captured": iso(cache.get("captured_at")), "windows": {w["key"]: {"used_pct": w["used"], "resets_at": iso(w["resets_at"])} for w in cache.get("windows") or []}}
    if CLAUDE_DESKTOP_DATA:
        samples = (read_json(CLAUDE_DESKTOP_DATA / "plan-usage-history.json") or {}).get("samples") or []
        last = sorted(samples, key=lambda s: s.get("t", 0))[-3:]
        out["desktop_samples_latest3"] = [{"at": iso(s.get("t", 0) / 1000), "five_hour": (s.get("u") or {}).get("fh"), "seven_day": (s.get("u") or {}).get("sd")} for s in last]
    else:
        out["desktop_samples_latest3"] = "no Claude-Data folder found"
    status = read_json(CLAUDE_STATUS)
    out["statusline"] = ({"captured": iso(status.get("captured_at")), "rate_limits": status.get("rate_limits")} if status else None)
    cdebug = {}
    info, err = _codex_fetch(cdebug) if codex_exe() else (None, "codex_missing")
    out["codex_live"] = {"exe": codex_exe(), "error": err, "response_keys": cdebug.get("raw_keys"), "response_shape": cdebug.get("shape"),
                         "windows": {w["key"]: {"used_pct": w["used"], "minutes": w["window_minutes"], "resets_at": iso(w["resets_at"])} for w in (info or {}).get("windows", [])},
                         "plan": (info or {}).get("plan")}
    sess = codex_snapshot()[1]
    out["codex_session_files"] = ({"captured": iso(sess["captured_at"]), "windows": {w["key"]: {"used_pct": w["used"], "resets_at": iso(w["resets_at"])} for w in sess["windows"]}} if sess else None)
    picked = claude_quota()
    out["island_uses"] = {"source": picked.get("source"), "captured": iso(picked.get("captured_at")),
                          "windows": {w["key"]: {"used_pct": w["used"], "resets_at": iso(w["resets_at"])} for w in picked["windows"]}}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


# ── Claude API spend (OpenCode) ──────────────────────────────────────────

def utc_day(ts):
    """Anthropic bills by UTC day/month, so API spend is bucketed in UTC to match the Console."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


PROVIDER_NAMES = {"anthropic": "Claude", "openai": "OpenAI / Codex", "google": "Gemini", "opencode": "OpenCode Zen",
                  "openrouter": "OpenRouter", "github-copilot": "Copilot", "xai": "xAI", "deepseek": "DeepSeek",
                  "mistral": "Mistral", "groq": "Groq", "azure": "Azure OpenAI", "amazon-bedrock": "Bedrock"}


def provider_name(pid):
    return PROVIDER_NAMES.get(pid) or str(pid).replace("-", " ").title()


def api_snapshot(events_only=False):
    """Local API spend per provider (from OpenCode's records). The top-level numbers are Claude's (anthropic).

    events_only: True -> anthropic's [(ts, cost), ...]; a provider id -> that provider's; no billing is computed."""
    now = time.time()
    today = utc_day(now)
    week = {utc_day(now - i * 86400) for i in range(7)}
    d30 = {utc_day(now - i * 86400) for i in range(30)}
    month = today[:7]
    daily = {utc_day(now - i * 86400): 0.0 for i in range(14)}
    sources, sessions, events = [], {}, []
    oc_events = {}   # session id -> [(ts, cumulative tokens, cumulative cost)]
    pstats = {}      # provider id -> {"stat": totals, "daily": {...}, "events": [(ts, cost)]}

    def per_provider(pid, ts, cost):
        b = pstats.setdefault(pid, {"stat": {"today": 0.0, "week": 0.0, "d30": 0.0, "month": 0.0, "total": 0.0},
                                    "daily": {utc_day(now - i * 86400): 0.0 for i in range(14)}, "events": []})
        _add_cost(b["stat"], b["daily"], cost, ts, today, week, d30, month)
        b["events"].append((ts, cost))

    def note(sid, ts, o):
        if not sid:
            return
        t = o.get("tokens") or o.get("usage") or {}
        if not isinstance(t, dict):
            return
        cache = t.get("cache") if isinstance(t.get("cache"), dict) else {}
        tok = sum(int(x or 0) for x in (t.get("input"), t.get("output"), cache.get("read", t.get("cacheRead")),
                                        cache.get("write", t.get("cacheWrite"))) if isinstance(x, (int, float)))
        cost = float(o.get("cost") or 0)
        if tok or cost:
            lst = oc_events.setdefault(sid, [])
            lst.append((ts, (lst[-1][1] if lst else 0) + tok, (lst[-1][2] if lst else 0.0) + cost))

    for label, db in opencode_dbs():
        if not db.exists():
            continue
        src = {"label": label, "today": 0.0, "week": 0.0, "d30": 0.0, "month": 0.0, "total": 0.0}
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            flavor = opencode_flavor(con)
            if flavor == "v1":
                try:
                    rows = con.execute("select time_created, data, session_id from message where data like '%\"cost\"%' order by time_created").fetchall()
                except sqlite3.OperationalError:      # no session_id column: per-call numbers are unavailable
                    rows = [(c, d, None) for c, d in con.execute("select time_created, data from message where data like '%\"cost\"%'")]
                for created, data, msg_sid in rows:
                    o = json.loads(data)
                    if o.get("role") == "assistant":
                        note(msg_sid, created / 1000, o)
                        if o.get("providerID"):
                            per_provider(o["providerID"], created / 1000, o.get("cost") or 0)
                    if o.get("role") != "assistant" or o.get("providerID") != "anthropic":
                        continue
                    _add_cost(src, daily, o.get("cost") or 0, created / 1000, today, week, d30, month)
                    events.append((created / 1000, o.get("cost") or 0))
                for sid, title, cost, ti, to, tr, tcr, tcw in con.execute(
                        "select id, title, cost, tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write from session"):
                    sessions[sid] = {"title": title, "cost": float(cost or 0),
                                     "tokens": sum(int(x or 0) for x in (ti, to, tcr, tcw))}
            elif flavor == "v2":
                try:
                    rows = con.execute("select time_created, data, session_id from session_message where type='assistant' and data like '%\"cost\"%' order by time_created").fetchall()
                except sqlite3.OperationalError:
                    rows = [(c, d, None) for c, d in con.execute("select time_created, data from session_message where type='assistant' and data like '%\"cost\"%'")]
                for created, data, msg_sid in rows:
                    o = json.loads(data)
                    note(msg_sid, created / 1000, o)
                    if (o.get("model") or {}).get("providerID"):
                        per_provider(o["model"]["providerID"], created / 1000, o.get("cost") or 0)
                    if (o.get("model") or {}).get("providerID") != "anthropic":
                        continue
                    _add_cost(src, daily, o.get("cost") or 0, created / 1000, today, week, d30, month)
                    events.append((created / 1000, o.get("cost") or 0))
                for sid, title, cost, ti, to, tcr, tcw in con.execute(
                        "select id, title, cost, tokens_input, tokens_output, tokens_cache_read, tokens_cache_write from session_v2"):
                    sessions[sid] = {"title": title, "cost": float(cost or 0),
                                     "tokens": sum(int(x or 0) for x in (ti, to, tcr, tcw))}
            con.close()
        except Exception as exc:
            src["error"] = str(exc)
        sources.append(src)
    for sid, evs in oc_events.items():
        if sid in sessions:
            sessions[sid]["events"] = evs
    if events_only:
        pid = "anthropic" if events_only is True else events_only
        return pstats.get(pid, {"events": []})["events"]
    total = {k: sum(s[k] for s in sources) for k in ("today", "week", "d30", "month", "total")}
    result = {"sources": sources, **total, "estimated": True,
              "daily": [{"day": d, "cost": round(v, 4)} for d, v in sorted(daily.items())]}
    billing = billing_snapshot(events)
    if billing.get("source") == "admin":
        # Exact figures from Anthropic replace the local estimate.
        result.update({k: billing[k] for k in ("today", "week", "d30", "month")}, estimated=False,
                      daily=billing["daily"])
    result["billing"] = billing
    # One entry per provider seen in OpenCode's records (Claude always first), each with its own balance estimate.
    config = read_json(CONFIG_FILE) or {}
    configured = set((config.get("provider_credit") or {}).keys())
    providers = [{"id": "anthropic", "name": provider_name("anthropic"), "today": result["today"], "week": result["week"],
                  "d30": result["d30"], "month": result["month"], "total": result["total"], "daily": result["daily"],
                  "billing": billing}]
    others = [(pid, b) for pid, b in pstats.items() if pid != "anthropic" and (b["stat"]["total"] > 0 or pid in configured)]
    others += [(pid, None) for pid in configured if pid != "anthropic" and pid not in pstats]
    for pid, b in sorted(others, key=lambda x: -(x[1]["stat"]["month"] if x[1] else 0)):
        st = b["stat"] if b else {"today": 0.0, "week": 0.0, "d30": 0.0, "month": 0.0, "total": 0.0}
        providers.append({"id": pid, "name": provider_name(pid), **{k: st[k] for k in ("today", "week", "d30", "month", "total")},
                          "daily": [{"day": d, "cost": round(v, 4)} for d, v in sorted((b["daily"] if b else {}).items())],
                          "billing": billing_snapshot(b["events"] if b else [], pid)})
    result["providers"] = providers
    return result, sessions


# ── Claude API balance: exact via the Admin Cost API, else a calibrated estimate ──

_cost_cache = {"at": 0.0, "days": None, "error": None}


def admin_daily_costs(key, since):
    """Daily USD cost per UTC day from GET /v1/organizations/cost_report (amounts are cents as decimal strings)."""
    if time.time() - _cost_cache["at"] < 300 and _cost_cache["days"] is not None:
        return _cost_cache["days"]
    days, page = {}, None
    end = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    try:
        for _ in range(40):
            query = f"starting_at={since}T00:00:00Z&ending_at={end}&bucket_width=1d&limit=31" + (f"&page={page}" if page else "")
            req = urllib.request.Request("https://api.anthropic.com/v1/organizations/cost_report?" + query, headers={
                "x-api-key": key, "anthropic-version": "2023-06-01", "User-Agent": "agent-island"})
            with urllib.request.urlopen(req, timeout=15) as response:
                data = json.loads(response.read())
            for bucket in data.get("data", []):
                day = (bucket.get("starting_at") or "")[:10]
                cents = sum(float(r.get("amount") or 0) for r in bucket.get("results", []))
                days[day] = days.get(day, 0.0) + cents / 100
            if not data.get("has_more"):
                break
            page = data.get("next_page")
        _cost_cache.update(at=time.time(), days=days, error=None)
    except Exception as exc:
        _cost_cache.update(at=time.time(), error=str(exc))
    return _cost_cache["days"]


def credit_cfg(config, pid):
    """(credit dict, monthly limit) for a provider. Claude keeps the original top-level keys."""
    if pid == "anthropic":
        return config.get("api_credit") or {}, config.get("api_monthly_limit")
    credit = (config.get("provider_credit") or {}).get(pid) or {}
    return credit, credit.get("monthly_limit")


def billing_snapshot(events, pid="anthropic"):
    config = read_json(CONFIG_FILE) or {}
    credit, limit = credit_cfg(config, pid)
    total = credit.get("total")
    now = time.time()
    month = utc_day(now)[:7]
    out = {"total": total, "monthly_limit": limit, "source": None}
    key = config.get("anthropic_admin_key") if pid == "anthropic" else None
    if key:
        days = admin_daily_costs(key, credit.get("since") or "2026-01-01")
        if days is not None:
            spent = sum(days.values())
            out.update(source="admin", spent=spent, as_of=_cost_cache["at"],
                       remaining=(total - spent) if total is not None else None,
                       today=days.get(utc_day(now), 0.0),
                       week=sum(days.get(utc_day(now - i * 86400), 0.0) for i in range(7)),
                       d30=sum(days.get(utc_day(now - i * 86400), 0.0) for i in range(30)),
                       month=sum(v for d, v in days.items() if d.startswith(month)),
                       daily=[{"day": utc_day(now - i * 86400), "cost": round(days.get(utc_day(now - i * 86400), 0.0), 4)}
                              for i in range(13, -1, -1)])
        else:
            out["error"] = _cost_cache["error"]
    snap = credit.get("snapshot")  # what the Console showed at a known moment
    if out["source"] is None and snap and snap.get("at"):
        at = iso_to_ts(snap["at"]) or 0
        # Local costs undercount real billing; scale by the factor learned between calibrations.
        factor = float(credit.get("factor") or 1.0)
        local = sum(cost for ts, cost in events if ts > at)
        since = local * factor
        same_month = utc_day(at)[:7] == month
        remaining = snap["balance"] - since if snap.get("balance") is not None else None
        age = now - at
        out.update(source="estimate", as_of=at, factor=factor, local_since=round(local, 4),
                   est_since=round(since, 4),
                   remaining=max(0.0, remaining) if remaining is not None else None,
                   month=(snap.get("month_spend", 0) + since) if same_month else
                         sum(cost for ts, cost in events if utc_day(ts)[:7] == month) * factor)
        # The estimate drifts: flag it once it is old or most of the estimate is extrapolation.
        if age > STALE_AFTER or (snap.get("balance") and since > 0.3 * snap["balance"]):
            out["stale"] = True
    if out.get("remaining") is not None and total:
        out["remaining_pct"] = max(0.0, min(100.0, out["remaining"] / total * 100))
    return out


STALE_AFTER = 6 * 3600            # an estimate older than this is shown as "please recalibrate"
FACTOR_RANGE = (1.0, 30.0)        # guard against nonsense factors from tiny samples
FACTOR_MIN_LOCAL = 0.5            # need at least this much locally recorded spend to learn a factor


def local_cost_events(pid="anthropic"):
    return api_snapshot(events_only=pid)


def calibrate(balance, month_spend=None, total=None, provider="anthropic"):
    """Record what the Console shows right now and learn how much local costs undercount real billing.

    factor = (previous balance − new balance) ÷ local spend recorded in between, blended with the
    previous factor so one noisy interval does not swing the estimate. Returns a summary dict.
    """
    balance = float(balance)
    if balance < 0:
        raise ValueError("balance must be >= 0")
    with _calibrate_lock:
        config = read_json(CONFIG_FILE) or {}
        credit = dict(credit_cfg(config, provider)[0])
        old = credit.get("snapshot") or {}
        old_factor = float(credit.get("factor") or 1.0)
        now = time.time()
        summary = {"provider": provider, "balance": balance, "factor": old_factor, "learned": None}
        at = iso_to_ts(old.get("at"))
        real = None
        if at and old.get("balance") is not None:
            local = sum(cost for ts, cost in local_cost_events(provider) if ts > at)
            real = float(old["balance"]) - balance
            summary.update(local=round(local, 4), real=round(real, 4))
            if real < -0.01:
                summary["note"] = "balance went up (top-up?) — factor kept"
                if total is None and credit.get("total") is not None:
                    credit["total"] = round(float(credit["total"]) - real, 2)  # count the top-up
            elif local >= FACTOR_MIN_LOCAL:
                learned = min(max(real / local, FACTOR_RANGE[0]), FACTOR_RANGE[1])
                factor = learned if "factor" not in credit else 0.7 * learned + 0.3 * old_factor
                summary.update(learned=round(learned, 2), factor=round(factor, 2))
                credit["factor"] = round(factor, 2)
            else:
                summary["note"] = f"local spend ${local:.2f} too small to learn a factor — kept"
        if month_spend is None:
            same_month = at and utc_day(at)[:7] == utc_day(now)[:7]
            if same_month and old.get("month_spend") is not None and real is not None and real > 0:
                month_spend = float(old["month_spend"]) + real
            elif not same_month:
                month_spend = None
            else:
                month_spend = old.get("month_spend")
        if total is not None:
            credit["total"] = float(total)
        snap = {"balance": round(balance, 2), "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if month_spend is not None:
            snap["month_spend"] = round(float(month_spend), 2)
        credit["snapshot"] = snap
        history = list(credit.get("history") or [])
        history.append({**snap, "factor": credit.get("factor", 1.0)})
        credit["history"] = history[-20:]
        if provider == "anthropic":
            config["api_credit"] = credit
        else:
            config.setdefault("provider_credit", {})[provider] = credit
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, CONFIG_FILE)
    with _ttl_lock:
        _ttl_cache.pop("api", None)
    summary["month_spend"] = snap.get("month_spend")
    return summary


_calibrate_lock = threading.Lock()


def _add_cost(src, daily, cost, ts, today, week, d30, month):
    day = utc_day(ts)
    src["total"] += cost
    if day == today:
        src["today"] += cost
    if day in week:
        src["week"] += cost
    if day in d30:
        src["d30"] += cost
    if day.startswith(month):
        src["month"] += cost
    if day in daily:
        daily[day] += cost


# ── What a running child is doing right now ──────────────────────────────

def _short(text, limit=90):
    return " ".join(str(text).split())[:limit] if text else ""


def codex_activity(path):
    """Latest step in a Codex session file: command, file edit, search, reasoning or reply."""
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(path) - 256 * 1024))
            lines = fh.read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"item_started"' not in line and '"item_completed"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        p = row.get("payload") or {}
        item, started = p.get("item") or {}, p.get("type") == "item_started"
        kind, at = item.get("type"), iso_to_ts(row.get("timestamp"))
        if kind == "CommandExecution":
            parsed = item.get("parsed_cmd") or []
            cmd = parsed[0].get("cmd") if parsed and isinstance(parsed[0], dict) else (item.get("command") or [""])[-1]
            text = ("正在运行 " if started else "运行了 ") + _short(cmd)
        elif kind == "FileChange":
            names = [os.path.basename(k) for k in (item.get("changes") or {})]
            text = ("正在修改 " if started else "修改了 ") + ", ".join(names[:3]) + (" 等" if len(names) > 3 else "")
        elif kind == "Extension":
            text = "查网页 " + _short(item.get("query"), 70)
        elif kind == "Reasoning":
            text = "思考中"
        elif kind == "AgentMessage":
            content = item.get("content") or []
            text = "回复：" + _short(next((c.get("text") for c in content if isinstance(c, dict) and c.get("text")), ""), 80)
        elif kind == "ContextCompaction":
            text = "压缩上下文"
        elif kind == "UserMessage":
            text = "收到新指令"
        else:
            continue
        return {"text": text, "at": at}
    return None


def opencode_activity(session_id):
    """Latest part (tool call, reasoning, text) of an OpenCode session, from whichever database has it."""
    for _, db in opencode_dbs():
        if not db.exists():
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
            tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
            table = "part" if "part" in tables else "session_part" if "session_part" in tables else None
            row = table and con.execute(f"select time_updated, data from {table} where session_id=? "
                                        f"order by time_updated desc limit 1", (session_id,)).fetchone()
            con.close()
        except sqlite3.Error:
            continue
        if not row:
            continue
        part, at = json.loads(row[1]), row[0] / 1000
        kind = part.get("type")
        if kind == "tool":
            state = part.get("state") or {}
            inp = state.get("input") or {}
            target = next((inp[k] for k in ("command", "filePath", "pattern", "url", "query", "description") if inp.get(k)), "")
            verb = "正在" if state.get("status") in ("running", "pending") else "完成"
            return {"text": f"{verb} {part.get('tool')} {_short(target, 80)}".strip(), "at": at}
        if kind == "text" and part.get("text"):
            return {"text": "回复：" + _short(part["text"], 80), "at": at}
        return {"text": "思考中", "at": at}
    return None


# ── AutoSwarm calls ──────────────────────────────────────────────────────

def _approval_summary(native):
    """Best-effort one-liner for a native approval request (shapes differ per provider)."""
    if not isinstance(native, dict):
        return None
    for key in ("command", "title", "permission", "tool_name", "reason", "message"):
        value = native.get(key)
        if isinstance(value, list):
            value = " ".join(map(str, value))
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:120]
    params = native.get("params") if isinstance(native.get("params"), dict) else None
    return _approval_summary(params) if params else None


def _inspect_all():
    if Records is None:
        return
    seen = set()
    for home in AUTOSWARM_HOMES:
        if not (home / "calls").exists():
            continue
        records = Records(home)
        for _, task in records.calls(skip_unreadable=True):
            if task["call_id"] in seen:
                continue
            seen.add(task["call_id"])
            try:
                yield task, swarm_engine.inspect_call(records, task["call_id"])
            except Exception as exc:
                yield task, {"state": "unknown", "error": str(exc)}


def calls_snapshot(codex_sessions, opencode_sessions):
    calls = []
    for task, info in _inspect_all():
        result = info.get("result")
        approvals = info.get("approvals") or []
        if info["state"] == "returned":
            state = (result or {}).get("outcome") or "completed"
        elif info["state"] == "running":
            state = "waiting" if approvals else "running"
        elif info["state"] == "interrupted":
            state = "lost"
        else:
            state = info["state"]
        session = info.get("native_session_id")
        created = iso_to_ts(task.get("created_at"))
        finished = iso_to_ts((result or {}).get("finished_at"))
        # Tokens of THIS call only: how much the conversation's counter grew while it ran. The same agent
        # (conversation) is reused for many rounds, so its running total says nothing about one round.
        usage = None
        end = finished or time.time()
        if task["provider"] == "codex" and session in codex_sessions and codex_sessions[session]["tokens"]:
            sess = codex_sessions[session]
            delta = gain(sess.get("events"), created or 0, end) if created else None
            usage = {"tokens": delta, "cost": None, "scope": "call", "session_tokens": sess["tokens"].get("total_tokens")} if delta is not None else \
                    {"tokens": sess["tokens"].get("total_tokens"), "cost": None, "scope": "session"}
        elif task["provider"] == "opencode" and session in opencode_sessions:
            s = opencode_sessions[session]
            if s.get("events") and created:
                usage = {"tokens": gain(s["events"], created, end, 1), "cost": gain(s["events"], created, end, 2), "scope": "call",
                         "session_tokens": s["tokens"]}
            else:
                usage = {"tokens": s["tokens"], "cost": s["cost"], "scope": "session"}
        activity = None
        if state in ("running", "waiting") and session:
            if task["provider"] == "codex" and session in codex_sessions:
                activity = codex_activity(codex_sessions[session]["path"])
            elif task["provider"] == "opencode":
                activity = opencode_activity(session)
        selection = task.get("native_options") or {}
        calls.append({
            "call_id": task["call_id"],
            "agent_id": task.get("agent_id") or task["call_id"],
            "provider": task["provider"],
            "name": (task.get("communication") or {}).get("name"),
            "model": task.get("model") or selection.get("model"),
            "effort": task.get("effort") or selection.get("variant"),
            "workspace": task.get("workspace"),
            "prompt": (task.get("prompt") or "").lstrip("\ufeff"),
            "content": (result or {}).get("content") or "",
            "error": (result or {}).get("error"),
            "state": state,
            "created_at": created,
            "finished_at": finished,
            "session": session,
            "usage": usage,
            "activity": activity,
            "approvals": [{"request_id": a.get("request_id"), "kind": a.get("kind"), "created_at": iso_to_ts(a.get("created_at")),
                           "summary": _approval_summary(a.get("native_request"))} for a in approvals],
            "probe": "AUTOSWARM_CHECK_" in (task.get("prompt") or ""),
        })
    calls.sort(key=lambda c: c["created_at"] or 0)
    rounds = {}
    probes = {c["agent_id"] for c in calls if c["probe"]}
    later = {}  # agent key -> newest call seen so far, walking backwards in time
    for c in calls:
        rounds[c["agent_id"]] = rounds.get(c["agent_id"], 0) + 1
        c["round"] = rounds[c["agent_id"]]
        c["probe"] = c["agent_id"] in probes
    # A failure is superseded once the same agent (same name, or the same conversation) was dispatched again.
    for c in reversed(calls):
        key = c["name"] or c["agent_id"]
        c["superseded"] = c["state"] in BAD_STATES and key in later
        later.setdefault(key, c)
        later.setdefault(c["agent_id"], c)
    calls.reverse()
    return calls


_ttl_cache = {}
_ttl_lock = threading.Lock()


def cached(key, ttl, fn):
    with _ttl_lock:
        hit = _ttl_cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _ttl_lock:
        _ttl_cache[key] = (time.time(), value)
    return value


BAD_STATES = ("failed", "lost", "cancelled", "unknown")

# ── Dismissed anomalies ───────────────────────────────────────────────────
# "Clearing" an anomaly only hides it here (island + dashboard share this list); AutoSwarm's own
# records are never touched, and anything can be restored.
DISMISSED_FILE = DATA / "island-dismissed.json"
DISMISSED_KEEP = 2000
_dismiss_lock = threading.Lock()


def dismissed_ids():
    value = read_json(DISMISSED_FILE)
    return [str(x) for x in value] if isinstance(value, list) else []


def set_dismissed(add=(), remove=(), clear=False):
    with _dismiss_lock:
        ids = [] if clear else dismissed_ids()
        drop = {str(x) for x in remove}
        ids = [x for x in ids if x not in drop]
        for x in map(str, add):
            if x not in ids:
                ids.append(x)
        ids = ids[-DISMISSED_KEEP:]
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = DISMISSED_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(ids), encoding="utf-8")
        os.replace(tmp, DISMISSED_FILE)
    return ids


def unresolved(c):
    """An anomaly still worth a look: failed / lost / cancelled, not retried since, not dismissed."""
    return c["state"] in BAD_STATES and not c.get("superseded") and not c.get("dismissed")


def codex_tokens(codex_sessions):
    """Codex tokens used today / in the last 7 local days, from the per-session counters."""
    now = time.time()
    today = local_day(now)
    week = {local_day(now - i * 86400) for i in range(7)}
    days = {}
    for sess in codex_sessions.values():
        for day, n in counter_days(sess.get("events")).items():
            days[day] = days.get(day, 0) + n
    return {"tokens_today": days.get(today, 0), "tokens_week": sum(n for d, n in days.items() if d in week)}


def _first_line(text, limit=90):
    for line in (text or "").lstrip("\ufeff").splitlines():
        if line.strip():
            line = line.strip()
            return line if len(line) <= limit else line[:limit] + "…"
    return ""


def delegation_summary(calls):
    """Everything dispatched so far, grouped by sub-agent (the name, or the conversation when unnamed)."""
    real = [c for c in calls if not c["probe"]]
    agents, today = {}, local_day(time.time())
    totals = {"calls": 0, "completed": 0, "failed": 0, "failed_total": 0, "running": 0, "tokens": 0, "cost": 0.0,
              "calls_today": 0, "tokens_today": 0, "by_provider": {}, "approx": False, "first_at": None}
    for c in sorted(real, key=lambda c: c["created_at"] or 0):
        key = f'{c["provider"]}|{c["name"] or c["agent_id"]}'
        a = agents.setdefault(key, {"key": key, "name": c["name"], "agent_id": c["agent_id"], "provider": c["provider"],
                                    "model": None, "effort": None, "rounds": 0, "completed": 0, "failed": 0, "failed_total": 0, "running": 0,
                                    "tokens": 0, "cost": 0.0, "approx": False, "first_at": c["created_at"], "last_at": None,
                                    "last_state": None, "last_task": ""})
        u = c.get("usage") or {}
        a["rounds"] += 1
        a["model"], a["effort"] = c["model"] or a["model"], c["effort"] or a["effort"]
        a["last_at"], a["last_state"], a["last_task"] = c["created_at"], c["state"], _first_line(c["prompt"])
        if c["state"] == "completed":
            a["completed"] += 1
        elif c["state"] in BAD_STATES:
            a["failed_total"] += 1
            if unresolved(c):
                a["failed"] += 1
        elif c["state"] in ("running", "waiting"):
            a["running"] += 1
        if u.get("scope") == "call" and u.get("tokens") is not None:
            a["tokens"] += u["tokens"]
            a["cost"] += u.get("cost") or 0.0
            if c["created_at"] and local_day(c["created_at"]) == today:
                totals["tokens_today"] += u["tokens"]
            bp = totals["by_provider"].setdefault(c["provider"], {"calls": 0, "tokens": 0, "cost": 0.0})
            bp["tokens"] += u["tokens"]
            bp["cost"] += u.get("cost") or 0.0
        elif u:
            a["approx"] = totals["approx"] = True       # only a conversation-wide total was available
            a["session_tokens"] = u.get("tokens")
        totals["by_provider"].setdefault(c["provider"], {"calls": 0, "tokens": 0, "cost": 0.0})["calls"] += 1
        totals["calls"] += 1
        totals["first_at"] = totals["first_at"] or c["created_at"]
        if c["created_at"] and local_day(c["created_at"]) == today:
            totals["calls_today"] += 1
    for a in agents.values():
        totals["completed"] += a["completed"]
        totals["failed"] += a["failed"]
        totals["failed_total"] += a["failed_total"]
        totals["running"] += a["running"]
        totals["tokens"] += a["tokens"]
        totals["cost"] += a["cost"]
    totals["agents"] = len(agents)
    return {"totals": totals, "agents": sorted(agents.values(), key=lambda a: a["last_at"] or 0, reverse=True)}


def snapshot():
    # Quotas and spend change slowly; delegation state is read fresh every time.
    codex_sessions, codex_limits = cached("codex", 20, codex_snapshot)
    codex_limits = {**codex_with_live(codex_limits), **codex_tokens(codex_sessions)}
    api, opencode_sessions = cached("api", 20, api_snapshot)
    calls = calls_snapshot(codex_sessions, opencode_sessions)
    dismissed = set(dismissed_ids())
    for c in calls:
        c["dismissed"] = c["call_id"] in dismissed
    today = local_day(time.time())
    real = [c for c in calls if not c["probe"]]
    return {
        "generated_at": time.time(),
        "claude": cached("claude", 10, claude_snapshot),
        "codex": codex_limits,
        "api": api,
        "calls": calls,
        "dismissed": sorted(dismissed),
        "restart_needed": restart_needed(),
        "delegation": delegation_summary(calls),
        "summary": {
            "calls_today": sum(1 for c in real if c["created_at"] and local_day(c["created_at"]) == today),
            "running": sum(1 for c in real if c["state"] == "running"),
            "waiting": sum(1 for c in real if c["state"] == "waiting"),
            "failed": sum(1 for c in real if unresolved(c)),
        },
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send((ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path.startswith("/island"):
            self._send((ROOT / "island.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path.startswith("/api/data"):
            if time.time() - _claude_state.get("last_scan", 0) > 60:
                _claude_state["last_scan"] = time.time()
                threading.Thread(target=claude_scan, daemon=True).start()
            try:
                body = json.dumps(snapshot(), ensure_ascii=False).encode("utf-8")
                self._send(body, "application/json; charset=utf-8")
            except Exception as exc:
                self._send(json.dumps({"error": str(exc)}).encode(), "application/json", 500)
        else:
            self._send(b"not found", "text/plain", 404)

    def do_POST(self):
        # Only the dashboard on this machine may write: reject cross-site requests from other pages.
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if self.path not in ("/api/calibrate", "/api/dismiss"):
            return self._send(b"not found", "text/plain", 404)
        if host.split(":")[0] not in ("127.0.0.1", "localhost") or (
                origin and origin.split("://")[-1].split(":")[0] not in ("127.0.0.1", "localhost")) or \
                "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._send(b'{"error":"forbidden"}', "application/json", 403)
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 65536)
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/dismiss":
                ids = lambda k: [str(x) for x in body.get(k) or [] if isinstance(x, (str, int))][:2000]
                add = ids("ids")
                if body.get("all_bad"):      # everything still showing as an anomaly right now
                    add += [c["call_id"] for c in snapshot()["calls"] if not c["probe"] and unresolved(c)]
                kept = set_dismissed(add=add, remove=ids("restore"), clear=bool(body.get("restore_all")))
                return self._send(json.dumps({"added": add, "dismissed": len(kept)}).encode(), "application/json; charset=utf-8")
            opt = lambda k: None if body.get(k) in (None, "") else float(body[k])
            provider = str(body.get("provider") or "anthropic")[:60]
            result = calibrate(float(body["balance"]), opt("month_spend"), opt("total"), provider)
            self._send(json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        except (KeyError, ValueError, TypeError) as exc:
            self._send(json.dumps({"error": f"bad input: {exc}"}).encode(), "application/json", 400)
        except Exception as exc:
            self._send(json.dumps({"error": str(exc)}).encode(), "application/json", 500)


# ── Has the code on disk changed since this process started? ──────────────
# After `git pull` / an update the page and island.html are read fresh, but this process keeps running
# the old Python. Tell the pages so they can ask for a restart instead of failing in confusing ways.
import hashlib

_CODE_FILES = [Path(__file__).resolve(), Path(__file__).resolve().with_name("island.py")]


def _code_state():
    parts = []
    for f in _CODE_FILES:
        try:
            st = f.stat()
            parts.append((st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append(None)
    return parts


def _code_hash():
    h = hashlib.sha1()
    for f in _CODE_FILES:
        try:
            h.update(f.read_bytes())
        except OSError:
            pass
    return h.hexdigest()


_code = {"state": _code_state(), "started": _code_hash(), "now": None}
_code["now"] = _code["started"]


def restart_needed():
    state = _code_state()
    if state != _code["state"]:                  # cheap stat check; re-hash only when a file was touched
        _code.update(state=state, now=_code_hash())
    return _code["now"] != _code["started"]


PORT = 8765


def start_server(port=PORT):
    """Serve in a background thread. Returns None when another instance already owns the port."""
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        return None
    DATA.mkdir(parents=True, exist_ok=True)
    _claude_load_cache()
    threading.Thread(target=claude_scan, daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--calibrate", type=float, metavar="BALANCE",
                        help="record the API balance the Console shows now (USD) and exit")
    parser.add_argument("--month", type=float, help="with --calibrate: this month's spend shown in the Console")
    parser.add_argument("--total", type=float, help="with --calibrate: total credit ever topped up")
    parser.add_argument("--provider", default="anthropic", help="with --calibrate: OpenCode provider id (anthropic, openai, ...)")
    parser.add_argument("--renew-token", action="store_true",
                        help="ask Claude Code to renew the terminal login token now and report which method worked")
    parser.add_argument("--quota-debug", action="store_true",
                        help="print where the Claude 5-hour / weekly numbers come from (no secrets) and exit")
    args = parser.parse_args()
    if args.renew_token:
        iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%m-%d %H:%M:%S") if ts else None
        r = renew_claude_token()
        r["before"], r["after"] = iso(r.get("before")), iso(r.get("after"))
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    if args.quota_debug:
        return quota_debug()
    if args.calibrate is not None:
        print(json.dumps(calibrate(args.calibrate, args.month, args.total, args.provider), ensure_ascii=False))
        return 0
    url = f"http://127.0.0.1:{args.port}/"
    server = start_server(args.port)
    if args.open:
        webbrowser.open(url)
    if server:
        print("Dashboard:", url, flush=True)
        threading.Event().wait()


if __name__ == "__main__":
    sys.exit(main())
