"""Routing decisions in-process, without any network."""

import time

from cswap_rotator.proxy import State

FAR = time.time() + 86400


class Creds:
    apikey = "k"

    def __init__(self, order=("1", "2"), active=""):
        self.order = list(order)
        self.active = active
        self.subs = {n: {"email": n, "token": "t" + n, "exp": time.time() + 9999} for n in order}

    def live_order(self):
        return self.subs, self.order


def test_account_that_read_full_stays_full_until_its_window_resets(tmp_path):
    st, creds = State(str(tmp_path / "s.json")), Creds()
    st.set_current("m", "sub:1")
    st.set_live("sub:1", {"7d": (95.5, FAR)}, {})
    first = st.candidates(creds, "m", usage={})[0]
    st.set_current("m", first)                      # a request succeeds on the new account
    st.set_live("sub:1", {"7d": (94.0, FAR)}, {})  # another reading says 94%
    assert first == "sub:2"
    assert st.candidates(creds, "m", usage={})[0] == "sub:2"
    st.set_current("m", "sub:1")                   # an old request finishing late
    assert st.current["m"] == "sub:2"


def test_full_account_returns_after_its_window_resets(tmp_path):
    st, creds = State(str(tmp_path / "s.json")), Creds()
    st.set_live("sub:1", {"7d": (95.5, time.time() - 1)}, {})
    st.full[("sub:1", "m")] = time.time() - 1
    assert "sub:1" in st.candidates(creds, "m", usage={})[:2]


def test_follows_cswap_then_grace_then_sticky_then_cswaps_next_pick(tmp_path):
    st, creds = State(str(tmp_path / "s.json")), Creds(order=("1", "2", "3"), active="2")
    st.set_current("m", "sub:1")
    st.set_live("sub:3", {"7d": (10.0, FAR)}, {})
    assert st.candidates(creds, "m", usage={})[0] == "sub:2"     # cswap's pick wins
    st.set_live("sub:2", {"7d": (96.0, FAR)}, {})
    assert st.candidates(creds, "m", usage={})[0] == "sub:2"     # just filled: grace
    st.full_since[("sub:2", "m")] = time.time() - 200
    assert st.candidates(creds, "m", usage={})[0] == "sub:1"     # grace over: sticky
    creds.active = "3"
    assert st.candidates(creds, "m", usage={})[0] == "sub:3"     # cswap switched


def test_api_key_comes_after_subscriptions_with_room_and_before_full_ones(tmp_path):
    st, creds = State(str(tmp_path / "s.json")), Creds(order=("1", "2"))
    st.set_live("sub:1", {"5h": (99.0, FAR)}, {})
    assert st.candidates(creds, "m", usage={}) == ["sub:2", "apikey", "sub:1"]
