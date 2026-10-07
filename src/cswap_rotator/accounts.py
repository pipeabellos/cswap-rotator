"""Read-only view of the logins cswap manages, through cswap's own code.

cswap (claude-swap) stays the single owner of credentials. This module never refreshes
or rotates a token (refresh tokens are single-use, so racing cswap would kill an
account) and never takes cswap's locks (every cswap writer publishes atomically, so
plain reads are safe). Reading through cswap keeps the storage details right on every
platform: macOS Keychain, the encrypted files cswap prefers when present, Linux and
Windows.

The proxy needs: each subscription's current access token, the Console API key, which
account cswap has active, and how full each account is.
"""

import json
import threading
import time

from . import config
from .usage import cswap_usage, log, windows_from_last_good


class HostMissing(RuntimeError):
    pass


INSTALL_HINT = ("cswap-rotator runs inside claude-swap's environment. Install it with:\n"
                "  uv tool install claude-swap --with cswap-rotator")


def _oauth(raw):
    """(access token, expiry epoch) from a stored OAuth credential, or None."""
    try:
        o = json.loads(raw)["claudeAiOauth"]
        return o["accessToken"], (o.get("expiresAt") or 0) / 1000.0
    except (ValueError, KeyError, TypeError):
        return None


class Credentials:
    """Snapshot of cswap's accounts, refreshed at most every CRED_TTL_S."""

    def __init__(self):
        self.lock = threading.Lock()
        self.loaded_at = 0.0
        self.subs = {}         # slot -> {"email", "token", "exp"}
        self.order = []        # cswap rotation order of subscription slots
        self.identities = {}   # slot -> (email, organizationUuid), to read usage rows
        self.apikey = None
        self.apikey_slot = None
        self.backend_name = "file" if config.CREDS_FILE else "cswap"
        self._sw = None
        self._active = ("", 0.0)
        self._active_test = ""

    # ---- cswap host
    def switcher(self):
        if self._sw is None:
            try:
                from claude_swap.switcher import ClaudeAccountSwitcher
            except ImportError as e:
                raise HostMissing(INSTALL_HINT) from e
            # One instance per process: construction runs cswap's own setup.
            self._sw = ClaudeAccountSwitcher()
        return self._sw

    @property
    def active(self):
        """The slot of the login cswap has active. Cached for 2s: it is read per request."""
        if config.CREDS_FILE:
            return self._active_test
        val, at = self._active
        if time.time() - at < 2:
            return val
        try:
            val = self.switcher().current_account_number() or ""
        except Exception:
            pass   # keep the last known answer
        self._active = (val, time.time())
        return val

    def live_order(self):
        """(subs, order) consistent with each other, safe while a refresh swaps them."""
        subs = self.subs
        return subs, [s for s in self.order if s in subs]

    def usage(self):
        """slot -> {"at", "windows"} from cswap's usage cache, identity-checked."""
        if config.CREDS_FILE:
            return cswap_usage(config.CSWAP_DIR)
        try:
            from claude_swap.usage_store import UsageStore
            sw = self.switcher()
            entries = UsageStore(sw.backup_dir / "cache").entries(dict(self.identities))
        except Exception:
            return {}
        out = {}
        for slot, e in entries.items():
            wins = windows_from_last_good(e.last_good)
            if wins:
                out[slot] = {"at": float(e.fetched_at or 0), "windows": wins}
        return out

    # ---- refresh
    def refresh(self, force=False):
        with self.lock:
            if not force and time.time() - self.loaded_at < config.CRED_TTL_S:
                return
            if config.CREDS_FILE:
                return self._refresh_from_file()
            try:
                sw = self.switcher()
                accounts = [(n, sw.account_identity(n), sw.account_kind_for(n))
                            for n in sw.switchable_account_numbers()]
            except HostMissing:
                raise
            except Exception as e:
                log("creds_error", error=repr(e))
                self.loaded_at = time.time() - config.CRED_TTL_S + 5   # retry soon, keep the pool
                return
            subs, order, identities = {}, [], {}
            apikey = apikey_slot = None
            failed = 0
            for n, ident, kind in accounts:
                email = ident.get("email", "")
                try:
                    raw = sw.read_account_credentials(n, email) or ""
                except Exception:
                    raw = ""
                if kind == "api_key":
                    if raw.startswith("sk-ant-api"):
                        if apikey is None:
                            apikey, apikey_slot = raw, n
                    elif self.apikey and apikey is None:
                        failed += 1   # transient read failure: keep the last good key
                        apikey, apikey_slot = self.apikey, self.apikey_slot
                    continue
                tok = _oauth(raw) if raw else None
                if tok is None:
                    if n in self.subs:
                        failed += 1   # keep the last good copy rather than drop the account
                        subs[n] = self.subs[n]
                        order.append(n)
                        identities[n] = self.identities.get(n, (email, ""))
                    continue
                subs[n] = {"email": email, "token": tok[0], "exp": tok[1]}
                order.append(n)
                identities[n] = (email, ident.get("organizationUuid", "") or "")
            # A slot that left the switchable list only because its stored copy was
            # momentarily unreadable stays; one removed or disabled in cswap goes.
            for n in self.order:
                if n in subs:
                    continue
                try:
                    keep = sw.account_identity(n).get("email") and not sw.is_account_disabled(n)
                except Exception:
                    keep = False
                if keep and n in self.subs:
                    failed += 1
                    subs[n], identities[n] = self.subs[n], self.identities.get(n, ("", ""))
                    order.append(n)
            if apikey is None and self.apikey and self.apikey_slot:
                try:
                    keep = (sw.account_identity(self.apikey_slot).get("email")
                            and not sw.is_account_disabled(self.apikey_slot))
                except Exception:
                    keep = False
                if keep:
                    failed += 1
                    apikey, apikey_slot = self.apikey, self.apikey_slot
            self._rescue_active(sw, subs)
            # subs before order: readers only use slots present in both (live_order)
            self.subs = subs
            self.order = order
            self.identities = identities
            self.apikey, self.apikey_slot = apikey, apikey_slot
            # after a partial failure, try again in a few seconds instead of a minute
            self.loaded_at = time.time() - (config.CRED_TTL_S - 5 if failed else 0)
            if failed:
                log("creds_partial", failed=failed)

    def _rescue_active(self, sw, subs):
        """If cswap's stored copy of the ACTIVE account has expired, use the live login.

        Only as a rescue: cswap keeps that copy fresh, and a live login read in the
        middle of a cswap switch could belong to the next account.
        """
        try:
            active = sw.current_account_number() or ""
        except Exception:
            return
        cur = subs.get(active)
        if not cur or cur["exp"] > time.time() + config.TOKEN_MIN_LIFE_S:
            return
        try:
            tok = _oauth(sw._read_credentials() or "")   # cswap's live-login reader
        except Exception:
            tok = None
        if tok and tok[1] > cur["exp"]:
            subs[active] = dict(cur, token=tok[0], exp=tok[1])

    def _refresh_from_file(self):
        with open(config.CREDS_FILE) as f:
            data = json.load(f)
        self.subs = {str(k): v for k, v in data.get("subs", {}).items()}
        self.order = [str(s) for s in data.get("order", list(self.subs))]
        self.identities = {s: (v.get("email", ""), "") for s, v in self.subs.items()}
        self.apikey = data.get("apikey")
        self.apikey_slot = "test" if self.apikey else None
        self._active_test = str(data.get("active") or "")
        self.loaded_at = time.time()
