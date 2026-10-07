"""cswap-rotator command line."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

from . import __version__, config

# A custom base URL makes Claude Code turn off MCP tool search (every MCP tool
# definition then loads upfront, often hundreds of thousands of tokens) and
# fine-grained tool streaming. The proxy forwards both, so keep them on.
PROXY_ENV = {
    "ANTHROPIC_BASE_URL": config.BASE_URL,
    "ENABLE_TOOL_SEARCH": "true",
    "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING": "1",
}


def _claude_dir():
    return os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))


def _settings_path():
    return os.path.join(_claude_dir(), "settings.json")


def _global_config_path():
    d = os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.join(os.path.expanduser(d), ".claude.json") if d else os.path.expanduser("~/.claude.json")


def short_name(email):
    """dev@acme.io -> acme, someone@gmail.com -> someone."""
    local, _, dom = (email or "?").partition("@")
    return local if dom in ("gmail.com", "googlemail.com", "outlook.com", "icloud.com") or not dom else dom.split(".")[0]


def _status_json(timeout=3):
    with urllib.request.urlopen(config.BASE_URL + "/rotator/status", timeout=timeout) as r:
        return json.load(r)


def _proxy_up():
    try:
        _status_json(timeout=2)
        return True
    except Exception:
        return False


def _write_settings(mutate):
    path = _settings_path()
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    mutate(data.setdefault("env", {}))
    if not data["env"]:
        data.pop("env")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".settings.", suffix=".json")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    if os.path.exists(path):
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
    os.replace(tmp, path)


def _cswap_api_key_fallback(include):
    """cswap must not switch to the API key itself while the proxy is on: that switch
    clears the subscription login and would log every proxied session out."""
    cswap = shutil.which("cswap")
    if not cswap:
        return "cswap not found on PATH; set autoswitch.includeApiKeyAccounts yourself"
    r = subprocess.run([cswap, "config", "set", "autoswitch.includeApiKeyAccounts",
                        "true" if include else "false"], capture_output=True, text=True)
    if r.returncode != 0:
        return f"could not update cswap ({r.stderr.strip() or r.stdout.strip()})"
    return (f"cswap autoswitch.includeApiKeyAccounts = {'true' if include else 'false'} "
            "(restart `cswap auto` if it is running)")


# ------------------------------------------------------------------ commands ---
def cmd_serve(args):
    from .proxy import serve
    serve()


def cmd_status(args):
    try:
        d = _status_json()
    except Exception:
        print(f"cswap-rotator is not running on {config.BASE_URL}.")
        return 1
    subs = {s["slot"]: s for s in d["subscriptions"]}
    print(f"cswap active account: {d.get('cswap_active') or '-'}   full at: {d.get('full_pct')}%")
    print("answering now (sessions on the proxy):")
    for model, key in sorted(d["current_by_model"].items()):
        if key == "apikey":
            who = "API key (Console credits)"
        else:
            s = subs.get(key[4:], {})
            u = s.get("usage_pct")
            who = f"{short_name(s.get('email'))} ({s.get('email', '?')})" + ("" if u is None else f", {u:.0f}% used")
        print(f"  {model:<28} {who}")
    print("cooldowns (s):", d["cooldowns_s"] or "-")
    print("api key:      ", f"slot {d['api_key']['slot']}" if d["api_key"]["present"] else "none")
    for s in d["subscriptions"]:
        u = s.get("usage_pct")
        print("  slot %3s %-14s %-32s %5s%s  token %sm" % (
            s["slot"], short_name(s["email"]), s["email"], "?" if u is None else "%.0f%%" % u,
            "  FULL" if s.get("full") else "      ", s["token_expires_in_min"]))
    return 0


def cmd_enable(args):
    if not _proxy_up() and not args.force:
        print(f"cswap-rotator is not running on {config.BASE_URL}; start it first "
              "(`cswap-rotator install-service`) or pass --force.")
        return 1
    print(_cswap_api_key_fallback(False))   # first, so cswap never logs a proxied session out
    _write_settings(lambda env: env.update(PROXY_ENV))
    print(f"New Claude Code sessions now go through {config.BASE_URL} ({_settings_path()}).")
    print("Sessions already running keep their old setup until restarted (claude --resume works).")
    return 0


def cmd_disable(args):
    _write_settings(lambda env: [env.pop(k, None) for k in PROXY_ENV])
    print(_cswap_api_key_fallback(True))
    print("New Claude Code sessions talk to Anthropic directly again.")
    print("Keep the proxy running until sessions started while it was enabled have exited.")
    return 0


def cmd_install_service(args):
    from . import service
    print(service.install())
    return 0


def cmd_uninstall_service(args):
    from . import service
    print(service.uninstall())
    return 0


def cmd_restart(args):
    from . import service
    service.restart()
    print("restarted")
    return 0


def cmd_statusline(args):
    """Print the account answering the session whose statusline JSON is on stdin."""
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    model = ((data.get("model") or {}).get("id") or "").split("[", 1)[0]
    color = (lambda code, s: f"\033[{code}m{s}\033[0m") if args.color else (lambda code, s: s)
    if (os.environ.get("ANTHROPIC_BASE_URL") or "").rstrip("/") != config.BASE_URL:
        try:
            with open(_global_config_path()) as f:
                email = (json.load(f).get("oauthAccount") or {}).get("emailAddress")
        except (OSError, ValueError):
            email = None
        if email:
            print(color("2", f"{short_name(email)} (direct)"), end="")
        return 0
    try:
        d = _status_json(timeout=0.3)
    except Exception:
        print(color("31", "⇄ proxy down"), end="")
        return 0
    key = d["current_by_model"].get(model)
    if key == "apikey":
        print(color("31", "⇄ API key"), end="")
    elif key:
        s = next((x for x in d["subscriptions"] if x["slot"] == key[4:]), {})
        u = s.get("usage_pct")
        print(color("32", f"⇄ {short_name(s.get('email'))}" + ("" if u is None else f" {u:.0f}%")), end="")
    else:
        print(color("32", "⇄ proxy"), end="")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="cswap-rotator",
        description="Per-request account rotation for Claude Code on top of claude-swap: "
                    "subscriptions first, the Console API key last, no session restarts.")
    p.add_argument("--version", action="version", version=f"cswap-rotator {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the proxy in the foreground").set_defaults(fn=cmd_serve)
    sub.add_parser("status", help="which account answers each model, usage per account").set_defaults(fn=cmd_status)
    e = sub.add_parser("enable", help="route new Claude Code sessions through the proxy")
    e.add_argument("--force", action="store_true", help="even if the proxy is not running yet")
    e.set_defaults(fn=cmd_enable)
    sub.add_parser("disable", help="new sessions talk to Anthropic directly again").set_defaults(fn=cmd_disable)
    sub.add_parser("install-service", help="start the proxy at login (launchd / systemd --user)").set_defaults(fn=cmd_install_service)
    sub.add_parser("uninstall-service", help="remove the login service").set_defaults(fn=cmd_uninstall_service)
    sub.add_parser("restart", help="restart the login service (sessions retry on their own)").set_defaults(fn=cmd_restart)
    s = sub.add_parser("statusline", help="print the answering account for a Claude Code statusline (JSON on stdin)")
    s.add_argument("--color", action="store_true", help="ANSI colors: green subscription, red API key")
    s.set_defaults(fn=cmd_statusline)
    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
