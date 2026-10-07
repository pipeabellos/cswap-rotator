"""End to end: a real cswap-rotator process in front of a fake Anthropic API."""

import concurrent.futures
import http.client
import json
import os
import time

from conftest import UA

API = "APIKEY:sk-ant-api-TESTKEY"
M = {"model": "opus"}


def test_first_subscription_with_oauth_beta_and_sticky(stack):
    status, data = stack.post(M)
    j = json.loads(data)
    assert status == 200 and j["who"] == "tokA"
    assert "oauth-2025-04-20" in j["beta"] and "some-beta" in j["beta"]
    assert stack.who(M) == (200, "tokA")


def test_quota_429_fails_over_and_parks_the_account(stack):
    stack.who(M)
    stack.ctrl(rejected=["tokA"], retry_after=600)
    assert stack.who(M) == (200, "tokB")          # the session never sees the 429
    stack.ctrl()                                   # tokA healthy upstream, still parked here
    assert stack.who(M) == (200, "tokB")
    assert any(k.startswith("sub:a|opus") for k in stack.status()["cooldowns_s"])


def test_all_subscriptions_out_of_quota_uses_api_key_without_oauth_beta(stack):
    stack.ctrl(rejected=["tokA", "tokB", "tokC"], retry_after=600)
    status, data = stack.post(M)
    j = json.loads(data)
    assert status == 200 and j["who"] == API
    assert "oauth" not in j["beta"] and "some-beta" in j["beta"]


def test_cooldowns_are_per_model(stack):
    stack.ctrl(rejected=["tokA"], retry_after=600)
    stack.who(M)                                   # parks tokA for opus only
    stack.ctrl()
    assert stack.who({"model": "haiku"}) == (200, "tokA")


def test_returns_to_a_subscription_when_its_cooldown_ends(stack):
    stack.ctrl(rejected=["tokA", "tokB", "tokC"], retry_after=1)
    assert stack.who(M) == (200, API)
    stack.ctrl()
    time.sleep(2.2)
    assert stack.who(M)[1].startswith("tok")


def test_401_on_a_subscription_fails_over(stack):
    stack.ctrl(badauth=["tokA"])
    assert stack.who(M) == (200, "tokB")


def test_everything_exhausted_returns_the_429(stack):
    stack.ctrl(rejected=["tokA", "tokB", "tokC", API], retry_after=600)
    assert stack.post(M)[0] == 429


def test_browser_and_dns_rebinding_requests_are_refused(stack):
    assert stack.post(M, headers={"origin": "https://evil.example"})[0] == 403
    c = http.client.HTTPConnection("127.0.0.1", stack.port)
    c.request("POST", "/v1/messages", body=b"{}", headers={"host": "evil.example"})
    assert c.getresponse().status == 403


def test_sse_stream_passes_through(stack):
    c = http.client.HTTPConnection("127.0.0.1", stack.port, timeout=10)
    c.request("POST", "/v1/messages", body=json.dumps({"model": "m", "stream": True}),
              headers={"content-type": "application/json", "authorization": "Bearer x", "user-agent": UA})
    r = c.getresponse()
    assert r.status == 200 and r.read().decode().count("event: content_block_delta") == 3


def test_subscription_at_95_percent_moves_to_another_subscription(stack):
    stack.ctrl(util={"tokA": {"5h": 0.10, "7d": 0.96}})
    assert stack.who(M)[1] == "tokA"               # it had room when picked
    assert stack.who(M)[1] == "tokB"               # its reply said 96%: full now


def test_every_subscription_full_uses_api_key(stack):
    stack.ctrl(util={"tokA": {"7d": 0.96}, "tokB": {"7d": 0.97}, "tokC": {"5h": 0.99}})
    assert [stack.who(M)[1] for _ in range(4)] == ["tokA", "tokB", "tokC", API]


def test_api_key_out_of_credits_squeezes_the_emptiest_full_subscription(stack):
    stack.ctrl(util={"tokA": {"7d": 0.96}, "tokB": {"7d": 0.97}, "tokC": {"5h": 0.99}})
    for _ in range(3):
        stack.who(M)
    stack.ctrl(util={"tokA": {"7d": 0.96}, "tokB": {"7d": 0.97}, "tokC": {"5h": 0.99}}, nocredit=[API])
    assert stack.who(M) == (200, "tokA")


def test_cswap_usage_cache_reset_window_counts_as_empty(stack):
    now = time.time()

    def iso(e):
        return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(e))

    def acct(p5, r5, p7):
        return {"fetchedAt": now + 5, "lastGood": {"five_hour": {"pct": p5, "resets_at": iso(r5)},
                                                   "seven_day": {"pct": p7, "resets_at": iso(now + 86400)}}}
    os.makedirs(os.path.join(stack.tmp, "cache"), exist_ok=True)
    with open(os.path.join(stack.tmp, "cache", "usage.json"), "w") as f:
        json.dump({"accounts": {"a": acct(99, now - 60, 10), "b": acct(5, now + 3600, 97),
                                "c": acct(5, now + 3600, 50)}}, f)
    assert stack.who({"model": "c1"})[1] == "tokA"   # its 5h window already reset
    full = {x["slot"]: x["full"] for x in stack.status()["subscriptions"]}
    assert full == {"a": False, "b": True, "c": False}


def test_forty_agents_streaming_at_once(stack):
    def one(_):
        c = http.client.HTTPConnection("127.0.0.1", stack.port, timeout=30)
        c.request("POST", "/v1/messages?beta=true", body=json.dumps({"model": "burst", "stream": True}),
                  headers={"content-type": "application/json", "authorization": "Bearer x", "user-agent": UA})
        r = c.getresponse()
        return r.status, r.read().decode().count("event: content_block_delta")
    with concurrent.futures.ThreadPoolExecutor(40) as ex:
        assert all(r == (200, 3) for r in ex.map(one, range(40)))


def test_other_clients_and_their_own_api_keys_pass_through(stack):
    sdk = {"user-agent": "Anthropic/Python 0.70.0", "authorization": "", "x-api-key": "app-own-key"}
    assert stack.who({"model": "app"}, headers=sdk)[1] == "APIKEY:app-own-key"
    own = {"authorization": "", "x-api-key": "launchctl-env-key"}
    assert stack.who({"model": "app"}, headers=own)[1] == "APIKEY:launchctl-env-key"


def test_session_holding_cswaps_own_api_key_is_rotated(stack):
    held = {"authorization": "", "x-api-key": "sk-ant-api-TESTKEY"}
    assert stack.who({"model": "app"}, headers=held)[1] == "tokA"


def test_request_specific_429_goes_to_api_key_after_two_subscriptions(stack):
    stack.ctrl(limited=["tokA", "tokB", "tokC"])   # 429 without a quota rejection
    assert stack.who({"model": "huge"}) == (200, API)
    assert [a[0] for a in stack.last_request()["attempts"]] == ["sub:a", "sub:b", "apikey"]
    assert not [k for k in stack.status()["cooldowns_s"] if k.endswith("|huge")]


def test_one_off_fallback_does_not_move_the_sticky_account(stack):
    stack.who({"model": "huge"})
    before = stack.status()["current_by_model"]["huge"]
    stack.ctrl(limited=["tokA", "tokB", "tokC"])
    stack.who({"model": "huge"})
    assert stack.status()["current_by_model"]["huge"] == before == "sub:a"


def test_restart_keeps_sticky_choice_and_quota_cooldowns(stack):
    stack.ctrl(rejected=["tokA"], retry_after=300)
    stack.who({"model": "q"})
    before = stack.status()
    stack.restart()
    after = stack.status()
    assert after["current_by_model"] == before["current_by_model"]
    assert "sub:a|q" in after["cooldowns_s"]


def test_network_error_returns_retryable_502_without_trying_other_accounts(make_stack):
    s = make_stack(upstream="http://127.0.0.1:9")   # nothing listens there
    assert s.post(M)[0] == 502
    assert len(s.last_request()["attempts"]) == 1


def test_follows_cswaps_active_account(make_stack):
    creds = {"subs": {n: {"email": f"{n}@x", "token": "tok" + n.upper(), "exp": time.time() + 99999}
                      for n in ("a", "b", "c")},
             "order": ["a", "b", "c"], "apikey": "sk-ant-api-TESTKEY", "active": "b"}
    s = make_stack(creds=creds)
    assert s.who(M) == (200, "tokB")
    assert s.status()["cswap_active"] == "b"


def test_status_never_exposes_secrets(stack):
    stack.who(M)
    text = json.dumps(stack.status())
    assert "tokA" not in text and "TESTKEY" not in text
