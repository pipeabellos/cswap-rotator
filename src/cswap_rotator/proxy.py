"""The local Anthropic API proxy.

Claude Code sessions point ANTHROPIC_BASE_URL here. For every inference request the
proxy picks the credential:

  1. A subscription cswap manages. It follows cswap's choice: cswap's active account
     whenever that account has room (FULL_PCT, 100 by default), so cswap, /status and
     every session agree;
     otherwise the account this model is already on (prompt caching and server-side
     thread state are per account), else the one with the most room. A subscription
     counts as full at FULL_PCT of any usage window that applies to the model, read
     live from each response's anthropic-ratelimit-unified-* headers and from cswap's
     usage cache.
  2. The Console API key (cswap's api_key slot) once every subscription is full.
  3. Full subscriptions, only if the API key is rate limited or out of credits.

Sessions never change auth mode, so moving between subscriptions and the API key
needs no restart. Only Claude Code requests that carry a subscription login (or
cswap's own API key) are re-authenticated; a request bringing any other API key passes
through untouched with that key.
"""

import http.client
import http.server
import json
import os
import socket
import socketserver
import ssl
import sys
import threading
import time
import urllib.parse

from . import config
from .accounts import Credentials
from .usage import live_windows, log, reset_seconds, unified_status, window_applies

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}
AUTH_HEADERS = {"x-api-key", "authorization"}


class State:
    """Routing memory: sticky account per model, cooldowns, full-account latches."""

    def __init__(self, state_path=None):
        self.state_path = state_path or config.STATE_PATH
        self.lock = threading.Lock()
        self.cooldown = {}   # (key, model) -> until epoch
        self.current = {}    # model -> key  (sticky choice)
        self.live = {}       # key -> {"at", "windows", "raw"} from the latest response
        # (key, model) -> until. Usage only rises inside a window, so once an account
        # reads full it stays full until that window resets. Without this latch two
        # sources disagreeing at the edge (live 94%, cache 95%) bounce every session
        # between accounts, and each bounce re-sends every conversation's context.
        self.full = {}
        self.full_since = {}  # (key, model) -> when it was latched (for FOLLOW_GRACE_S)
        self.counts = {}      # key -> {"ok": n, "429": n, "auth": n, "other": n}

    # ---- persistence
    def save(self):
        now = time.time()
        with self.lock:
            snap = {"current": dict(self.current),
                    "cooldown": [[k, m, u] for (k, m), u in self.cooldown.items() if u > now],
                    "full": [[k, m, u] for (k, m), u in self.full.items() if u > now]}
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(snap, f)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def load(self):
        try:
            with open(self.state_path) as f:
                snap = json.load(f)
        except (OSError, ValueError):
            return
        now = time.time()
        with self.lock:
            self.current = {str(m): str(k) for m, k in (snap.get("current") or {}).items()}
            self.cooldown = {(k, m): u for k, m, u in snap.get("cooldown") or [] if u > now}
            self.full = {(k, m): u for k, m, u in snap.get("full") or [] if u > now}

    # ---- bookkeeping
    def bump(self, key, kind):
        with self.lock:
            c = self.counts.setdefault(key, {"ok": 0, "429": 0, "auth": 0, "other": 0})
            c[kind] = c.get(kind, 0) + 1

    def cool(self, key, model, seconds, reason):
        seconds = max(config.MIN_COOLDOWN_S, min(config.MAX_COOLDOWN_S, seconds))
        with self.lock:
            self.cooldown[(key, model)] = time.time() + seconds
            if self.current.get(model) == key:
                self.current.pop(model, None)
        log("cooldown", key=key, model=model, seconds=int(seconds), reason=reason)
        self.save()

    def cooling(self, key, model, now):
        return self.cooldown.get((key, model), 0) > now

    def set_live(self, key, windows, raw):
        with self.lock:
            self.live[key] = {"at": time.time(), "windows": windows, "raw": raw}

    def latched(self, key, model, now=None):
        with self.lock:
            return self.full.get((key, model), 0) > (now or time.time())

    # ---- how full is an account
    def utilization(self, slot, model, usage, now):
        return self.utilization_detail(slot, model, usage, now)[0]

    def utilization_detail(self, slot, model, usage, now):
        """(highest % across the windows that apply to `model`, that window's reset).

        Sources: cswap's usage cache and the live headers; the newer one wins per window.
        """
        sources = []
        if usage.get(slot):
            sources.append(usage[slot])
        with self.lock:
            lv = self.live.get("sub:" + slot)
        if lv and now - lv["at"] < config.LIVE_MAX_AGE_S:
            sources.append(lv)
        merged = {}
        for src in sorted(sources, key=lambda x: x["at"]):
            merged.update(src["windows"])
        m = (model or "").lower()
        vals = [(0.0 if reset and reset <= now else pct, reset)   # window already reset
                for win, (pct, reset) in merged.items() if window_applies(win, m)]
        return max(vals) if vals else (None, 0.0)

    # ---- routing
    def candidates(self, creds, model, usage=None):
        """Credential keys to try for one request, best first ("sub:N" or "apikey")."""
        now = time.time()
        usage = creds.usage() if usage is None else usage
        subs, order = creds.live_order()
        usable = [
            s for s in order
            if subs[s]["exp"] > now + config.TOKEN_MIN_LIFE_S
            and not self.cooling("sub:" + s, model, now)
        ]
        util, new_latch = {}, False
        for s in usable:
            pct, reset = self.utilization_detail(s, model, usage, now)
            if pct is not None and pct >= config.FULL_PCT and not self.latched("sub:" + s, model, now):
                with self.lock:
                    self.full[("sub:" + s, model)] = reset if reset > now else now + config.DEFAULT_COOLDOWN_S
                    self.full_since[("sub:" + s, model)] = now
                log("full", key="sub:" + s, model=model, pct=pct, until=int(reset) if reset > now else None)
                new_latch = True
            util[s] = 100.0 if self.latched("sub:" + s, model, now) else pct
        if new_latch:
            self.save()
        active = creds.active or ""
        with self.lock:
            cur = self.current.get(model)
            since = self.full_since.get(("sub:" + active, model), 0)
        # Subscriptions with room first: cswap's active account (so everything agrees
        # with cswap), then the one this model is already on, then the most headroom.
        # Then the API key. Full subscriptions go last, only for when the API key is
        # limited or out of credits. A subscription that drops back under FULL_PCT
        # (window reset) outranks the API key again on the very next request.
        room = [s for s in usable if util[s] is None or util[s] < config.FULL_PCT]
        # Right after cswap's account fills, keep it a little longer: cswap (polling
        # about every minute) makes the next pick, so sessions move once, not twice.
        if active in usable and active not in room and now - since < config.FOLLOW_GRACE_S:
            room.append(active)
        room.sort(key=lambda s: (0 if s == active else (1 if cur == "sub:" + s else 2),
                                 50.0 if util[s] is None else util[s], order.index(s)))
        full = sorted((s for s in usable if s not in room), key=lambda s: (util[s], order.index(s)))
        keys = ["sub:" + s for s in room]
        if creds.apikey and not self.cooling("apikey", model, now):
            keys.append("apikey")
        return keys + ["sub:" + s for s in full]

    def set_current(self, model, key):
        # A request that started before its account filled up can finish after the
        # move; it must not drag every session back onto the full account.
        if key != "apikey" and self.latched(key, model):
            return
        with self.lock:
            prev = self.current.get(model)
            self.current[model] = key
        if prev != key:
            log("switch", model=model, frm=prev, to=key)
            self.save()


CREDS = Credentials()
STATE = State()


def _shape(req):
    """Request settings that can make a subscription refuse it. Never message content."""
    if not isinstance(req, dict):
        return {}
    out = {k: req[k] for k in ("max_tokens", "stream", "speed", "service_tier", "temperature")
           if k in req and not isinstance(req[k], (dict, list))}
    out["keys"] = sorted(req)
    out["n_tools"] = len(req.get("tools") or [])
    out["n_messages"] = len(req.get("messages") or [])
    return out


def _err_text(data):
    try:
        e = json.loads(data or b"{}").get("error") or {}
        return f'{e.get("type", "")}: {e.get("message", "")}'[:300]
    except (ValueError, AttributeError):
        return (data or b"")[:200].decode("utf-8", "replace")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cswap-rotator"

    def log_message(self, *a):
        pass

    # ---- plumbing
    def _read_body(self):
        if self.headers.get("transfer-encoding", "").lower() == "chunked":
            out = bytearray()
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    break
                out += self.rfile.read(size)
                self.rfile.readline()
            return bytes(out)
        n = int(self.headers.get("content-length") or 0)
        return self.rfile.read(n) if n else b""

    def _send_json(self, status, obj):
        body = json.dumps(obj, indent=2, default=str).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _guard(self):
        # Browsers always send Origin on cross-site requests; Claude Code never does.
        # This keeps a web page from spending quota through the localhost proxy.
        if self.headers.get("origin"):
            self._send_json(403, {"error": "browser requests are not allowed"})
            return False
        host = (self.headers.get("host") or "").lower()
        if host not in (f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}"):
            self._send_json(403, {"error": "bad host"})   # DNS-rebinding guard
            return False
        return True

    def _upstream(self, method, path, body, headers):
        up = config.UPSTREAM
        if up.scheme == "https":
            conn = http.client.HTTPSConnection(up.hostname, up.port or 443,
                                               timeout=config.UPSTREAM_TIMEOUT_S,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(up.hostname, up.port or 80,
                                              timeout=config.UPSTREAM_TIMEOUT_S)
        conn.request(method, path, body=body, headers=headers)
        return conn, conn.getresponse()

    def _base_headers(self, body):
        out = {}
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in HOP_BY_HOP or kl in ("host", "content-length", "accept-encoding"):
                continue
            out[k] = v
        out["host"] = config.UPSTREAM.netloc
        out["content-length"] = str(len(body))
        out["accept-encoding"] = "identity"
        return out

    def _stream_back(self, resp):
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            kl = k.lower()
            if kl in HOP_BY_HOP or kl in ("content-length", "content-encoding"):
                continue
            self.send_header(k, v)
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        while True:
            chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
            if not chunk:
                break
            self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _send_buffered(self, status, headers, data):
        self.send_response(status)
        for k, v in headers:
            kl = k.lower()
            if kl in HOP_BY_HOP or kl in ("content-length", "content-encoding"):
                continue
            self.send_header(k, v)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- request routing
    def _handle(self):
        if not self._guard():
            return
        path_only = self.path.split("?", 1)[0]
        if path_only == "/rotator/status" and self.command == "GET":
            return self._status()
        if path_only == "/rotator/cooldown" and self.command == "POST":
            return self._admin_cooldown()
        body = self._read_body()
        if self.command == "POST" and path_only in config.INFERENCE_PATHS:
            why = self._passthrough_reason()
            if why is None:
                return self._inference(body)
            log("passthrough_inference", reason=why, ua=(self.headers.get("user-agent") or "")[:80])
        return self._passthrough(body)

    def _passthrough_reason(self):
        """None when this request's credential is ours to swap, else why it is not.

        Claude Code (CLI and Agent SDK) sends user-agent claude-cli/<v> and x-app: cli.
        A subscription login arrives as a Bearer token; cswap's managed key as x-api-key.
        """
        ua = (self.headers.get("user-agent") or "").lower()
        if not (ua.startswith("claude-cli/") or self.headers.get("x-app") == "cli"):
            return "not-claude-code"
        key = self.headers.get("x-api-key")
        if key:
            CREDS.refresh()
            if key != CREDS.apikey:
                return "own-api-key"
        return None

    def _passthrough(self, body):
        """Non-inference endpoints keep the session's own credential untouched."""
        conn = None
        try:
            conn, resp = self._upstream(self.command, self.path, body, self._base_headers(body))
            log("passthrough", method=self.command, path=self.path.split("?", 1)[0], status=resp.status)
            self._stream_back(resp)
        except (OSError, http.client.HTTPException) as e:
            self._send_json(502, {"error": f"upstream: {e}"})
        finally:
            if conn:
                conn.close()

    def _inference(self, body):
        try:
            req = json.loads(body or b"{}")
            model = req.get("model") or "*"
        except (ValueError, AttributeError):
            req, model = {}, "*"
        CREDS.refresh()
        keys = STATE.candidates(CREDS, model)
        if not keys:
            log("no_credentials", model=model)
            return self._send_json(503, {"type": "error", "error": {
                "type": "overloaded_error",
                "message": "cswap-rotator: no subscription or API key available"}})
        t0 = time.time()
        attempts = []
        last = None   # (status, headers, data) of the last failure, returned if all fail
        soft429 = 0   # 429s that were about this request, not an exhausted account
        for i, key in enumerate(keys):
            if soft429 >= config.SOFT_429_SKIP and key != "apikey":
                continue   # every subscription refuses this request the same way
            # last = nothing left that this request would still try
            is_last = not [k for k in keys[i + 1:] if soft429 < config.SOFT_429_SKIP or k == "apikey"]
            headers = self._base_headers(body)
            for h in list(headers):
                if h.lower() in AUTH_HEADERS or h.lower() == "anthropic-beta":
                    headers.pop(h)
            betas = [b.strip() for b in (self.headers.get("anthropic-beta") or "").split(",")
                     if b.strip() and not b.strip().startswith("oauth-")]
            if key == "apikey":
                if not CREDS.apikey:
                    continue
                headers["x-api-key"] = CREDS.apikey
            else:
                tok = (CREDS.subs.get(key[4:]) or {}).get("token")
                if not tok:
                    continue
                headers["authorization"] = "Bearer " + tok
                betas.append(config.OAUTH_BETA)
            if betas:
                headers["anthropic-beta"] = ",".join(betas)
            conn = None
            try:
                conn, resp = self._upstream("POST", self.path, body, headers)
            except (OSError, http.client.HTTPException) as e:
                # The network, not the account: moving to another account would only
                # cost every moved session its thread state and prompt cache. Hand
                # Claude Code a retryable 502 and it retries the same request.
                if conn:
                    conn.close()
                attempts.append((key, f"neterr:{type(e).__name__}"))
                log("request", model=model, path=self.path.split("?", 1)[0], used=key, status=502,
                    attempts=attempts, ms=int((time.time() - t0) * 1000), kb=len(body) // 1024)
                return self._send_json(502, {"type": "error", "error": {
                    "type": "api_error",
                    "message": f"cswap-rotator: network error reaching Anthropic ({type(e).__name__}), retry"}})
            status = resp.status
            if key != "apikey":
                windows = live_windows(resp.getheaders())
                if windows:
                    STATE.set_live(key, windows, {k: v for k, v in resp.getheaders()
                                                  if k.lower().startswith("anthropic-ratelimit-unified")})
            data = None
            no_credit = False
            if key == "apikey" and status == 400:
                data = resp.read()   # small JSON error; "credit balance is too low" = out of credits
                no_credit = b"credit balance" in data.lower()
            failover = status == 429 or no_credit or (status in (401, 403) and key != "apikey")
            if failover and not is_last:
                if data is None:
                    data = resp.read()
                conn.close()
                if status == 429 and unified_status(resp.getheaders()).get("status") == "rejected":
                    # The account is out of quota: park it for this model until its reset.
                    STATE.cool(key, model, reset_seconds(resp.getheaders(), config.DEFAULT_COOLDOWN_S), "429")
                    STATE.bump(key, "429")
                elif status == 429:
                    # The account still has quota, so the refusal is about THIS request (a
                    # huge uncached prompt, a burst throttle). Fail it over without parking
                    # the account: one oversized request must not move every session.
                    if key != "apikey":
                        soft429 += 1
                    STATE.bump(key, "429")
                elif no_credit:
                    STATE.cool(key, model, config.NO_CREDIT_COOLDOWN_S, "no-credit")
                    STATE.bump(key, "other")
                else:
                    STATE.cool(key, model, config.AUTH_FAIL_COOLDOWN_S, f"http{status}")
                    STATE.bump(key, "auth")
                    CREDS.refresh(force=True)
                attempts.append((key, status, _err_text(data), unified_status(resp.getheaders())))
                last = (status, resp.getheaders(), data)
                continue
            # Success, or the last candidate's answer: hand it to the session as-is.
            if status >= 400 and data is None:
                data = resp.read()   # error bodies are small JSON
            if soft429:
                log("soft429", model=model, served_by=key, status=status, shape=_shape(req),
                    betas=self.headers.get("anthropic-beta"), ua=(self.headers.get("user-agent") or "")[:60])
            if status < 400:
                # A one-off fallback (this request refused by subscriptions that still
                # have quota) must not move the sticky account for everyone else.
                if not soft429:
                    STATE.set_current(model, key)
                STATE.bump(key, "ok")
            elif status == 429:
                if unified_status(resp.getheaders()).get("status") == "rejected":
                    STATE.cool(key, model, reset_seconds(resp.getheaders(), config.DEFAULT_COOLDOWN_S), "429-last")
                STATE.bump(key, "429")
            else:
                STATE.bump(key, "other")
            attempts.append((key, status) if status < 400
                            else (key, status, _err_text(data), unified_status(resp.getheaders())))
            try:
                if data is not None:
                    self._send_buffered(status, resp.getheaders(), data)
                else:
                    self._stream_back(resp)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                conn.close()
            log("request", model=model, path=self.path.split("?", 1)[0], used=key,
                status=status, attempts=attempts, ms=int((time.time() - t0) * 1000), kb=len(body) // 1024,
                betas=self.headers.get("anthropic-beta") if status >= 400 else None)
            return
        if last:   # every candidate failed over; return the final failure
            self._send_buffered(*last)

    # ---- observability / ops
    def _status(self):
        CREDS.refresh()
        now = time.time()
        usage = CREDS.usage()
        with STATE.lock:
            cooldowns = {f"{k}|{m}": int(u - now) for (k, m), u in STATE.cooldown.items() if u > now}
            current = dict(STATE.current)
            counts = {k: dict(v) for k, v in STATE.counts.items()}
            live = {k: v["raw"] for k, v in STATE.live.items()}
        subs, order = CREDS.live_order()
        util = {s: STATE.utilization(s, "", usage, now) for s in order}
        self._send_json(200, {
            "listen": f"{config.HOST}:{config.PORT}",
            "upstream": config.UPSTREAM.geturl(),
            "full_pct": config.FULL_PCT,
            "cswap_active": CREDS.active,
            "current_by_model": current,
            "cooldowns_s": cooldowns,
            "subscriptions": [
                {"slot": s, "email": subs[s]["email"],
                 "token_expires_in_min": int((subs[s]["exp"] - now) / 60),
                 "usage_pct": util[s],
                 "full": util[s] is not None and util[s] >= config.FULL_PCT}
                for s in order
            ],
            "api_key": {"slot": CREDS.apikey_slot, "present": bool(CREDS.apikey)},
            "counts": counts,
            "live_ratelimit_headers": live,
        })

    def _admin_cooldown(self):
        """POST /rotator/cooldown?key=sub:N|apikey|all-subs&model=<id>&seconds=N (ops, tests)."""
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        self._read_body()
        secs = float(q.get("seconds", 60))
        model = q.get("model", "*")
        CREDS.refresh()
        keys = (["sub:" + s for s in CREDS.live_order()[1]] if q.get("key") == "all-subs"
                else [q.get("key", "")])
        for k in keys:
            STATE.cool(k, model, secs, "admin")
        self._send_json(200, {"cooled": keys, "model": model, "seconds": secs})

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _handle


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128   # many agents can connect at once

    def server_bind(self):
        # HTTPServer.server_bind resolves its own name with socket.getfqdn(), a reverse
        # DNS lookup that can hang for a long time on some Macs (GitHub's macOS runners
        # do). The socket is already listening by then, so sessions connect but get no
        # answer. Skip it: the name is never used.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, socket.timeout)):
            return
        log("handler_error", error=repr(exc))


def serve():
    os.makedirs(os.path.dirname(config.LOG_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(config.STATE_PATH), exist_ok=True)
    CREDS.refresh(force=True)
    STATE.load()
    log("start", listen=f"{config.HOST}:{config.PORT}", upstream=config.UPSTREAM.geturl(),
        subscriptions=CREDS.live_order()[1], api_key=bool(CREDS.apikey), backend=CREDS.backend_name)
    Server((config.HOST, config.PORT), Handler).serve_forever()
