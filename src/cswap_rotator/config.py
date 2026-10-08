"""Settings, read once from the environment when the proxy starts.

Every knob has a CSWAP_ROTATOR_* variable so tests and unusual setups can override it
without touching code. Defaults are what a normal cswap install needs.
"""

import os
import urllib.parse


def _env(name, default):
    return os.environ.get("CSWAP_ROTATOR_" + name, default)


HOST = "127.0.0.1"
PORT = int(_env("PORT", "8890"))
BASE_URL = f"http://{HOST}:{PORT}"
UPSTREAM = urllib.parse.urlparse(_env("UPSTREAM", "https://api.anthropic.com"))


def _cswap_root():
    """cswap's data directory: ~/.claude-swap-backup on macOS and Windows, the XDG data
    dir on Linux. Asked from cswap itself when it is installed alongside."""
    try:
        from claude_swap.paths import get_backup_root
        return str(get_backup_root())
    except Exception:
        return os.path.expanduser("~/.claude-swap-backup")


# CSWAP_DIR overrides it for tests and relocated installs.
CSWAP_DIR = os.path.expanduser(os.environ.get("CSWAP_DIR") or _cswap_root())
HOME_DIR = os.path.expanduser(_env("HOME_DIR", os.path.join(CSWAP_DIR, "rotator")))
LOG_PATH = os.path.expanduser(_env("LOG", os.path.join(HOME_DIR, "rotator.log")))
# Sticky choices, quota cooldowns and full-account latches survive restarts: a restart
# that re-picked accounts would make every session rebuild its context elsewhere.
STATE_PATH = os.path.expanduser(_env("STATE", os.path.join(HOME_DIR, "state.json")))
# Tests supply credentials from a JSON file instead of cswap's store.
CREDS_FILE = _env("CREDS_FILE", None)

# A subscription counts as full at this % of any usage window that applies to the
# model. 100 uses every subscription until Anthropic refuses it: the proxy then
# retries that same request on the next account, so sessions never see the limit
# and no buffer is needed. Lower it only to keep headroom on purpose.
FULL_PCT = float(_env("FULL_PCT", "100"))
# Lowered only in tests.
MIN_COOLDOWN_S = float(_env("MIN_COOLDOWN_S", "60"))

INFERENCE_PATHS = ("/v1/messages", "/v1/messages/count_tokens", "/v1/complete")
OAUTH_BETA = "oauth-2025-04-20"
CRED_TTL_S = 60              # re-read cswap's stored logins at most this often
TOKEN_MIN_LIFE_S = 90        # skip subscription tokens about to expire
DEFAULT_COOLDOWN_S = 900     # quota 429 that carries no reset time
SOFT_429_SKIP = 2            # after this many non-quota 429s, a request goes to the API key
FOLLOW_GRACE_S = 180         # stay on cswap's account this long after it fills
AUTH_FAIL_COOLDOWN_S = 300   # after a 401/403 on a subscription
MAX_COOLDOWN_S = 7 * 86400
LIVE_MAX_AGE_S = 900         # trust a response's live utilization headers this long
NO_CREDIT_COOLDOWN_S = 3600  # Console API key answered "credit balance is too low"
ACCOUNT_WINDOWS = ("5h", "7d")  # usage windows shared by every model on an account
UPSTREAM_TIMEOUT_S = 900
LOG_MAX_BYTES = 20 * 1024 * 1024
