"""How full each subscription is: cswap's usage cache plus live response headers."""

import datetime
import json
import os
import threading
import time

from . import config

_log_lock = threading.Lock()


def log(event, **fields):
    """Append one JSON line to the rotator log (rotated at LOG_MAX_BYTES)."""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    line = json.dumps(rec, default=str)
    with _log_lock:
        try:
            path = config.LOG_PATH
            if os.path.exists(path) and os.path.getsize(path) > config.LOG_MAX_BYTES:
                os.replace(path, path + ".1")
            with open(path, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass


def _epoch(iso):
    try:
        return datetime.datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


def windows_from_last_good(good):
    """{window: (pct, reset_epoch)} from one cswap usage row's lastGood."""
    good = good if isinstance(good, dict) else {}
    wins = {}
    for src, name in (("five_hour", "5h"), ("seven_day", "7d")):
        w = good.get(src) or {}
        if isinstance(w.get("pct"), (int, float)):
            wins[name] = (float(w["pct"]), _epoch(w.get("resets_at")))
    for w in good.get("scoped") or []:
        if isinstance(w, dict) and w.get("name") and isinstance(w.get("pct"), (int, float)):
            wins[str(w["name"]).lower()] = (float(w["pct"]), _epoch(w.get("resets_at")))
    return wins


def cswap_usage(cswap_dir):
    """slot -> {"at", "windows"} read straight from <cswap_dir>/cache/usage.json.

    Used by tests and as a fallback; with cswap installed the proxy reads the same
    file through cswap's identity-checked UsageStore (accounts.Credentials.usage).
    Windows are "5h" and "7d" (account wide) plus lower-cased per-model scopes such as
    "fable". cswap polls inactive accounts only every few minutes, so live response
    headers win whenever they are newer.
    """
    try:
        with open(os.path.join(cswap_dir, "cache", "usage.json")) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for slot, entry in (d.get("accounts") or {}).items():
        entry = entry or {}
        wins = windows_from_last_good(entry.get("lastGood"))
        if wins:
            out[str(slot)] = {"at": float(entry.get("fetchedAt") or 0), "windows": wins}
    return out


def live_windows(headers):
    """{window: (pct, reset_epoch)} from anthropic-ratelimit-unified-<w>-utilization (0..1)."""
    h = {k.lower(): v for k, v in headers}
    pre, suf = "anthropic-ratelimit-unified-", "-utilization"
    out = {}
    for k, v in h.items():
        if not (k.startswith(pre) and k.endswith(suf)):
            continue
        win = k[len(pre):-len(suf)].lower()
        try:
            pct = float(v) * 100.0
        except ValueError:
            continue
        try:
            reset = float(h.get(pre + win + "-reset") or 0)
        except ValueError:
            reset = 0.0
        out[win] = (pct, reset)
    return out


def window_applies(win, model):
    """Account windows apply to every model; a scoped one ("fable") only to that model."""
    if win in config.ACCOUNT_WINDOWS:
        return True
    tokens = [t for t in "".join(c if c.isalpha() else " " for c in win).split() if len(t) >= 4]
    return any(t in model for t in tokens)


def unified_status(headers):
    """The account-level verdict headers, e.g. {'status': 'rejected', '7d-status': 'rejected'}."""
    pre = "anthropic-ratelimit-unified-"
    return {k.lower()[len(pre):]: v for k, v in headers
            if k.lower().startswith(pre) and k.lower().endswith("status")}


def reset_seconds(headers, default):
    """Seconds until the account recovers, from retry-after / unified reset headers."""
    h = {k.lower(): v for k, v in headers}
    best = None
    ra = h.get("retry-after")
    if ra:
        try:
            best = float(ra)
        except ValueError:
            pass
    for k in ("anthropic-ratelimit-unified-reset", "anthropic-ratelimit-tokens-reset",
              "anthropic-ratelimit-requests-reset"):
        v = h.get(k)
        if not v:
            continue
        try:
            secs = float(v) - time.time()          # epoch seconds
        except ValueError:
            try:                                    # RFC 3339
                secs = _epoch(v.replace("Z", "+00:00")) - time.time()
            except ValueError:
                continue
        if secs > 0:
            best = max(best or 0, secs)
    return best if best else default
