"""Reading cswap's accounts through a fake cswap switcher (no real Keychain or files)."""

import json
import time

import pytest

from cswap_rotator import config
from cswap_rotator.accounts import Credentials


def oauth(token, ttl=9999):
    return json.dumps({"claudeAiOauth": {"accessToken": token, "expiresAt": (time.time() + ttl) * 1000}})


class FakeSwitcher:
    """The handful of public cswap calls the rotator uses."""

    def __init__(self):
        self.accounts = {"1": ("a@x", "org-a", None), "2": ("b@x", "org-b", None),
                         "3": ("k@x", "org-k", "api_key")}
        self.stored = {"1": oauth("t1"), "2": oauth("t2"), "3": "sk-ant-api-K"}
        self.unreadable = set()
        self.disabled = set()
        self.active = "1"
        self.live = None

    def switchable_account_numbers(self):
        return [n for n in self.accounts if n not in self.disabled and n not in self.unreadable]

    def account_identity(self, n):
        email, org, _ = self.accounts.get(n, ("", "", None))
        return {"email": email, "organizationUuid": org, "uuid": ""}

    def account_kind_for(self, n):
        return self.accounts[n][2] or "oauth"

    def is_account_disabled(self, n):
        return n in self.disabled

    def read_account_credentials(self, n, email):
        return "" if n in self.unreadable else self.stored.get(n, "")

    def current_account_number(self):
        return self.active

    def _read_credentials(self):
        return self.live


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setattr(config, "CREDS_FILE", None)
    c = Credentials()
    c.backend_name = "cswap"
    c._sw = FakeSwitcher()
    c.refresh(force=True)
    return c


def test_reads_subscriptions_and_api_key_through_cswap(creds):
    assert creds.order == ["1", "2"]
    assert creds.subs["2"]["token"] == "t2"
    assert creds.apikey == "sk-ant-api-K"
    assert creds.identities["1"] == ("a@x", "org-a")
    assert creds.active == "1"


def test_unreadable_login_keeps_last_good_copy_instead_of_dropping_it(creds):
    creds._sw.unreadable = {"2", "3"}
    creds.refresh(force=True)
    assert creds.order == ["1", "2"] and creds.subs["2"]["token"] == "t2"
    assert creds.apikey == "sk-ant-api-K"
    assert time.time() - creds.loaded_at > config.CRED_TTL_S - 10   # retried within seconds


def test_disabled_or_removed_account_leaves_the_pool(creds):
    creds._sw.disabled = {"2"}
    creds.refresh(force=True)
    assert creds.order == ["1"]
    creds._sw.disabled = set()
    del creds._sw.accounts["1"]
    creds.refresh(force=True)
    assert creds.order == ["2"]


def test_expired_stored_copy_of_active_account_is_rescued_from_live_login(creds):
    creds._sw.stored["1"] = oauth("old", ttl=-10)
    creds._sw.live = oauth("live")
    creds.refresh(force=True)
    assert creds.subs["1"]["token"] == "live"


def test_fresh_stored_copy_is_never_replaced_by_live_login(creds):
    creds._sw.live = oauth("live", ttl=99999)
    creds.refresh(force=True)
    assert creds.subs["1"]["token"] == "t1"
