"""Unit tests for solmix using a local mock Solana RPC and a fake socks5 path."""
import json
import threading
import time
import http.server
import socketserver
import urllib.request
import sys
import types

# fake PySocks so tests run without the mixnet: connections go direct
fake_socks = types.ModuleType("socks")
fake_socks.PROXY_TYPE_SOCKS5 = 2
fake_socks.setdefaultproxy = lambda *a, **k: None
fake_socks.socksocket = __import__("socket").socket
sys.modules["socks"] = fake_socks

import solmix

class MockRPC(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        req = json.loads(body)
        if isinstance(req, list):
            resp = [{"jsonrpc": "2.0", "id": r.get("id"), "result": {"ok": r.get("method")}} for r in req]
        else:
            resp = {"jsonrpc": "2.0", "id": req.get("id"),
                    "result": {"ok": req.get("method"), "params": req.get("params")}}
        out = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

mock = ThreadingHTTPServer(("127.0.0.1", 0), MockRPC)
mock_port = mock.server_address[1]
threading.Thread(target=mock.serve_forever, daemon=True).start()
solmix.UPSTREAMS = [f"http://127.0.0.1:{mock_port}"]

# 1. single call
r = solmix.call_via_mixnet("getBalance", ["SomeAddress111"])
assert r["result"]["ok"] == "getBalance" and r["result"]["params"] == ["SomeAddress111"], r
print("PASS single call")

# 2. batch passthrough
batch = json.dumps([
    {"jsonrpc": "2.0", "id": 10, "method": "getHealth", "params": []},
    {"jsonrpc": "2.0", "id": 11, "method": "getEpochInfo", "params": []},
]).encode()
r = solmix.call_via_mixnet(None, None, _raw=batch)
assert [x["id"] for x in r] == [10, 11] and r[1]["result"]["ok"] == "getEpochInfo", r
print("PASS batch passthrough")

# 3. gateway end-to-end
gw = solmix.ThreadingHTTPServer(("127.0.0.1", 0), solmix.GatewayHandler)
gw_port = gw.server_address[1]
threading.Thread(target=gw.serve_forever, daemon=True).start()
payload = json.dumps({"jsonrpc": "2.0", "id": 99, "method": "getBlockHeight", "params": []}).encode()
resp = json.loads(urllib.request.urlopen(
    urllib.request.Request(f"http://127.0.0.1:{gw_port}", data=payload,
                           headers={"Content-Type": "application/json"}), timeout=10).read())
assert resp["id"] == 99 and resp["result"]["ok"] == "getBlockHeight", resp
print("PASS gateway end-to-end (id preserved)")

# 4. gateway batch
resp = json.loads(urllib.request.urlopen(
    urllib.request.Request(f"http://127.0.0.1:{gw_port}", data=batch,
                           headers={"Content-Type": "application/json"}), timeout=10).read())
assert [x["id"] for x in resp] == [10, 11], resp
print("PASS gateway batch")

gw.shutdown(); mock.shutdown()
print("ALL TESTS PASS")
