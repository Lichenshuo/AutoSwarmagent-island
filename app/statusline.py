"""Claude Code statusLine: saves rate limits for the dashboard and prints a short line."""
import json
import sys
import time
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "claude-status.json"


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except Exception:
        data = {}
    limits = data.get("rate_limits") or {}
    model = (data.get("model") or {}).get("display_name") or "Claude"
    if limits:
        try:
            OUT.parent.mkdir(parents=True, exist_ok=True)
            snapshot = {
                "captured_at": time.time(),
                "model": model,
                "rate_limits": limits,
                "session_cost_usd": (data.get("cost") or {}).get("total_cost_usd"),
            }
            tmp = OUT.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot), encoding="utf-8")
            tmp.replace(OUT)
        except Exception:
            pass
    parts = [model]
    five = (limits.get("five_hour") or {}).get("used_percentage")
    week = (limits.get("seven_day") or {}).get("used_percentage")
    if five is not None:
        parts.append(f"5h {five:.0f}%")
    if week is not None:
        parts.append(f"周 {week:.0f}%")
    sys.stdout.buffer.write(" · ".join(parts).encode("utf-8"))


if __name__ == "__main__":
    main()
