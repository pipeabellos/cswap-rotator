import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pytest

# Point everything at a scratch directory BEFORE cswap_rotator is imported anywhere, so
# no test can read real accounts or write into a real rotator log.
_SCRATCH = tempfile.mkdtemp(prefix="cswap-rotator-tests-")
os.environ["CSWAP_DIR"] = _SCRATCH
os.environ["CSWAP_ROTATOR_HOME_DIR"] = _SCRATCH

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
UA = "claude-cli/9.9.9 (external, cli)"   # the proxy only re-authenticates Claude Code


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return          # it answered
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"{url} did not come up")


class Stack:
    """A mock Anthropic API plus a cswap-rotator in front of it, both subprocesses."""

    def __init__(self, tmp, creds=None, upstream=None):
        self.tmp = str(tmp)
        self.ctrl_path = os.path.join(self.tmp, "ctrl.json")
        self.ctrl()
        creds = creds or {"subs": {n: {"email": f"{n}@x", "token": "tok" + n.upper(), "exp": time.time() + 99999}
                                   for n in ("a", "b", "c")},
                          "order": ["a", "b", "c"], "apikey": "sk-ant-api-TESTKEY"}
        self.creds_path = os.path.join(self.tmp, "creds.json")
        with open(self.creds_path, "w") as f:
            json.dump(creds, f)
        self.mock_port, self.port = _free_port(), _free_port()
        self.log_path = os.path.join(self.tmp, "rotator.log")
        self.state_path = os.path.join(self.tmp, "state.json")
        self.procs = []
        if upstream is None:
            self.procs.append(subprocess.Popen([sys.executable, os.path.join(HERE, "mock_upstream.py"),
                                                str(self.mock_port), self.ctrl_path]))
            upstream = f"http://127.0.0.1:{self.mock_port}"
        self.env = dict(os.environ, PYTHONPATH=SRC, CSWAP_DIR=self.tmp,
                        CSWAP_ROTATOR_PORT=str(self.port), CSWAP_ROTATOR_UPSTREAM=upstream,
                        CSWAP_ROTATOR_CREDS_FILE=self.creds_path, CSWAP_ROTATOR_MIN_COOLDOWN_S="1",
                        CSWAP_ROTATOR_LOG=self.log_path, CSWAP_ROTATOR_STATE=self.state_path,
                        CSWAP_ROTATOR_HOME_DIR=self.tmp)
        self.rotator = None
        self.start()
        if upstream.endswith(str(self.mock_port)):
            _wait_http(f"http://127.0.0.1:{self.mock_port}/")

    def start(self):
        self.rotator = subprocess.Popen([sys.executable, "-m", "cswap_rotator", "serve"], env=self.env)
        _wait_http(f"http://127.0.0.1:{self.port}/rotator/status")

    def restart(self):
        self.rotator.terminate()
        self.rotator.wait()
        self.start()

    def stop(self):
        for p in [self.rotator] + self.procs:
            if p:
                p.terminate()
                p.wait()

    def ctrl(self, **kw):
        c = {"limited": [], "rejected": [], "badauth": [], "nocredit": [], "util": {}, "retry_after": 2}
        c.update(kw)
        with open(self.ctrl_path, "w") as f:
            json.dump(c, f)

    def post(self, body, headers=None, path="/v1/messages?beta=true"):
        h = {"content-type": "application/json", "authorization": "Bearer sk-ant-oat01-session-login",
             "anthropic-beta": "some-beta,oauth-2025-04-20", "user-agent": UA}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v != ""}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
                                     headers=h, method="POST")
        try:
            r = urllib.request.urlopen(req, timeout=20)
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def who(self, body, **kw):
        status, data = self.post(body, **kw)
        return status, json.loads(data).get("who")

    def status(self):
        return json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/rotator/status", timeout=5))

    def cool(self, key, model, seconds):
        urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{self.port}/rotator/cooldown?key={key}&model={model}&seconds={seconds}",
            data=b"", method="POST"), timeout=5)

    def last_request(self):
        with open(self.log_path) as f:
            lines = [json.loads(line) for line in f if '"event": "request"' in line]
        return lines[-1]


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path)
    yield s
    s.stop()


@pytest.fixture
def make_stack(tmp_path):
    made = []

    def make(**kw):
        sub = tmp_path / f"s{len(made)}"
        sub.mkdir()
        s = Stack(sub, **kw)
        made.append(s)
        return s
    yield make
    for s in made:
        s.stop()
