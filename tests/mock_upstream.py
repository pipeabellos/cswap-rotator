"""A fake Anthropic API. Behaviour per credential comes from a ctrl.json file that is
re-read on every request, so a test can change it between calls.

ctrl keys (all optional):
  limited:  credentials answered with a 429 that is NOT a quota rejection
  rejected: credentials answered with a quota 429 (unified-status: rejected)
  badauth:  credentials answered with 401
  nocredit: API keys answered with 400 "credit balance is too low"
  util:     {credential: {window: fraction}} sent back as live usage headers
  retry_after: seconds for retry-after / the unified reset
"""

import http.server
import json
import socketserver
import sys
import time

CTRL = sys.argv[2]


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        with open(CTRL) as f:
            ctrl = json.load(f)
        auth = self.headers.get("authorization", "")
        key = self.headers.get("x-api-key", "")
        beta = self.headers.get("anthropic-beta", "")
        who = auth[7:] if auth.startswith("Bearer ") else ("APIKEY:" + key if key else "NONE")

        def send(status, obj, extra=()):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            for k, v in extra:
                self.send_header(k, v)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        ra = ctrl.get("retry_after", 2)
        if who in ctrl.get("badauth", []):
            return send(401, {"type": "error", "error": {"type": "authentication_error"}, "who": who})
        if who in ctrl.get("rejected", []):
            return send(429, {"type": "error", "error": {"type": "rate_limit_error"}, "who": who},
                        [("anthropic-ratelimit-unified-status", "rejected"),
                         ("anthropic-ratelimit-unified-reset", str(int(time.time()) + ra))])
        if who in ctrl.get("limited", []):
            return send(429, {"type": "error", "error": {"type": "rate_limit_error"}, "who": who},
                        [("retry-after", str(ra))])
        if who in ctrl.get("nocredit", []):
            return send(400, {"type": "error", "error": {
                "type": "invalid_request_error",
                "message": "Your credit balance is too low to access the Anthropic API."}, "who": who})
        util = []
        for win, frac in (ctrl.get("util", {}).get(who) or {}).items():
            util += [(f"anthropic-ratelimit-unified-{win}-utilization", str(frac)),
                     (f"anthropic-ratelimit-unified-{win}-reset", str(ctrl.get("reset", 4102444800)))]
        req = json.loads(body or b"{}")
        if req.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            for k, v in util:
                self.send_header(k, v)
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            for i in range(3):
                ev = f"event: content_block_delta\ndata: {json.dumps({'i': i, 'who': who})}\n\n".encode()
                self.wfile.write(b"%x\r\n" % len(ev) + ev + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            return
        send(200, {"ok": True, "who": who, "beta": beta, "model": req.get("model")}, util)


class Server(http.server.ThreadingHTTPServer):
    def server_bind(self):   # skip the reverse DNS lookup, see cswap_rotator.proxy.Server
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


Server(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
