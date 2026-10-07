"""Run the proxy at login: a launchd agent on macOS, a systemd user unit on Linux."""

import os
import plistlib
import shutil
import subprocess
import sys

from . import config

LABEL = "io.github.cswap-rotator"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
UNIT = os.path.expanduser("~/.config/systemd/user/cswap-rotator.service")


def _command():
    # The interpreter cswap-rotator is installed in, so the service survives PATH changes.
    return [sys.executable, "-m", "cswap_rotator", "serve"]


def install():
    os.makedirs(config.HOME_DIR, exist_ok=True)
    if sys.platform == "darwin":
        os.makedirs(os.path.dirname(PLIST), exist_ok=True)
        with open(PLIST, "wb") as f:
            plistlib.dump({
                "Label": LABEL,
                "ProgramArguments": _command(),
                "RunAtLoad": True,
                "KeepAlive": True,
                "ThrottleInterval": 10,
                "StandardOutPath": os.path.join(config.HOME_DIR, "service.out"),
                "StandardErrorPath": os.path.join(config.HOME_DIR, "service.err"),
            }, f)
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, PLIST], check=True)
        return f"launchd agent {LABEL} installed and started ({PLIST})"
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        os.makedirs(os.path.dirname(UNIT), exist_ok=True)
        with open(UNIT, "w") as f:
            f.write(
                "[Unit]\nDescription=cswap-rotator (Claude Code account proxy)\n\n"
                "[Service]\n"
                f"ExecStart={' '.join(_command())}\n"
                "Restart=always\nRestartSec=5\n\n"
                "[Install]\nWantedBy=default.target\n"
            )
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "cswap-rotator"], check=True)
        return f"systemd user unit installed and started ({UNIT})"
    raise SystemExit(
        "No service manager support on this platform. Start it at login yourself with:\n"
        f"  {' '.join(_command())}"
    )


def uninstall():
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        if os.path.exists(PLIST):
            os.remove(PLIST)
        return "launchd agent removed"
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "disable", "--now", "cswap-rotator"], capture_output=True)
        if os.path.exists(UNIT):
            os.remove(UNIT)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        return "systemd user unit removed"
    return "nothing to remove on this platform"


def restart():
    """Restart the installed service. Safe while sessions run: an in-flight request is
    cut, and Claude Code retries it on its own."""
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"], check=True)
    elif sys.platform.startswith("linux") and shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "restart", "cswap-rotator"], check=True)
    else:
        raise SystemExit("restart the `cswap-rotator serve` process yourself on this platform")
