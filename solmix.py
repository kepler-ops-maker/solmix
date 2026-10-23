#!/usr/bin/env python3
"""
solmix - Solana JSON-RPC over the Nym mixnet.

A local JSON-RPC gateway + CLI that routes Solana RPC traffic through the
Nym mixnet (via Nym's official nym-socks5-client), so RPC providers and
network observers never see your IP address.

  solmix setup             pick a random high-performance exit gateway, init + start nym-socks5-client
  solmix stop              stop the background nym-socks5-client
  solmix status            show client state (socks5 bind, exit provider, mixnet address)
  solmix gateway [port]    serve Solana JSON-RPC at 127.0.0.1:port (default 8899)
  solmix <method> [params] one-shot JSON-RPC call through the mixnet (e.g. solmix getBalance <addr>)
  solmix ip                show the IP address RPC providers see (a Nym exit gateway)

Point any Solana wallet or CLI at http://127.0.0.1:8899 and all of its RPC
traffic exits the mixnet from a Nym exit gateway instead of your connection.
TLS is end-to-end between you and the RPC provider; the exit sees only
host:port and ciphertext. DNS is resolved by the exit (no local resolver
leaks).

MIT licensed. No accounts, no API keys, no tracking. stdlib + PySocks only;
the mixnet itself is driven by Nym's official release binary.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import random
import re
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

NYM_API = "https://validator.nymtech.net/api"
MIN_PERFORMANCE = 0.95
CLIENT_ID = "solmix"
SOCKS5_PORT = 1080
DEFAULT_GATEWAY_PORT = 8899
UPSTREAMS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
    "https://rpc.ankr.com/solana",
]
IP_ECHO = "https://api.ipify.org?format=json"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


# ---------------------------------------------------------------- nym client

def nym_home() -> Path:
    return Path(os.environ.get("NYM_HOME", Path.home() / ".nym"))


def state_dir() -> Path:
    d = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "solmix"
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_binary() -> str:
    for cand in (os.environ.get("SOLMIX_NYM_SOCKS5"), shutil.which("nym-socks5-client"),
                 str(Path.home() / ".solmix" / "nym-socks5-client")):
        if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
            return str(cand)
    sys.exit("nym-socks5-client not found. Download a nym-binaries release from "
             "https://github.com/nymtech/nym/releases, verify sha256 against the "
             "release's hashes.json, chmod +x, then set SOLMIX_NYM_SOCKS5 or place "
             "it at ~/.solmix/nym-socks5-client. See README.")


def api_get(path: str):
    req = urllib.request.Request(NYM_API.rstrip("/") + path, headers={"User-Agent": "solmix"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def list_exit_providers(min_performance: float = MIN_PERFORMANCE) -> list[dict]:
    """Active exit gateways that run a network requester applying the Nym exit policy.

    This queries the public nym-api directly, exactly like nym-socks5-client
    does to fetch the network topology: it reveals that you use Nym, nothing else.
    """
    skimmed = api_get("/v1/unstable/nym-nodes/skimmed/exit-gateways/all")["nodes"]["data"]
    described = {n["node_id"]: n["description"]
                 for n in api_get("/v1/nym-nodes/described?per_page=5000")["data"]}
    out = []
    for n in skimmed:
        if n.get("role") != "ExitGateway" or float(n.get("performance", 0)) < min_performance:
            continue
        d = described.get(n["node_id"]) or {}
        nr = d.get("network_requester") or {}
        if not nr.get("address") or not nr.get("uses_exit_policy"):
            continue
        out.append({
            "node_id": n["node_id"],
            "country": (d.get("auxiliary_details") or {}).get("location"),
            "performance": float(n["performance"]),
            "version": (d.get("build_information") or {}).get("build_version", "?"),
            "address": nr["address"],
        })
    return out


def config_path() -> Path:
    return nym_home() / "socks5-clients" / CLIENT_ID / "config" / "config.toml"


def patch_config() -> None:
    """Some client releases write a config missing credential_requests_database."""
    p = config_path()
    text = p.read_text()
    if re.search(r"^credential_requests_database\s*=", text, re.M):
        return
    m = re.search(r"^credentials_database = '(.*)/credentials_database\.db'\s*$", text, re.M)
    if m:
        p.write_text(text[:m.end()] + "\n"
                     f"credential_requests_database = '{m.group(1)}/credential_requests.db'"
                     + text[m.end():])


def pidfile() -> Path:
    return state_dir() / "nym-socks5-client.pid"


def logfile() -> Path:
    return state_dir() / "nym-socks5-client.log"


def running_pid() -> int | None:
    try:
        pid = int(pidfile().read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def cmd_setup() -> None:
    binary = find_binary()
    providers = list_exit_providers()
    if not providers:
        sys.exit("nym-api lists no active exit gateway with a network requester right now")
    provider = random.choice(providers)  # random: spreads load, keeps choice unpredictable
    print(f"exit gateway: node {provider['node_id']} "
          f"({provider['country'] or '?'}, perf {provider['performance']:.2f}, v{provider['version']})")
    if not config_path().exists():
        out = subprocess.run(
            [binary, "init", "--id", CLIENT_ID, "--provider", provider["address"],
             "--port", str(SOCKS5_PORT), "--use-reply-surbs", "true"],
            capture_output=True, text=True, timeout=300)
        text = _ANSI.sub("", out.stdout + out.stderr)
        if out.returncode != 0:
            sys.exit(f"nym-socks5-client init failed:\n{text[-1500:]}")
        patch_config()
        print("client initialised (reply SURBs on: the exit does not learn your mixnet address)")
    else:
        print("client already initialised; keeping existing provider")
    patch_config()
    if running_pid():
        print("already running:", status_info()["socks5"])
        return
    fh = open(logfile(), "w")
    proc = subprocess.Popen([binary, "run", "--id", CLIENT_ID],
                            stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
    pidfile().write_text(str(proc.pid))
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            sys.exit(f"nym-socks5-client exited early:\n"
                     f"{_ANSI.sub('', logfile().read_text())[-1500:]}")
        if "Client startup finished" in logfile().read_text():
            info = status_info()
            print(f"ready: socks5 {info['socks5']}, mixnet address {info['client_address']}")
            print("next: solmix ip   (or)   solmix gateway   (or)   solmix getHealth")
            return
        time.sleep(0.5)
    sys.exit(f"client not ready after 180s; see {logfile()}")


def cmd_stop() -> None:
    pid = running_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        pidfile().unlink(missing_ok=True)
        print("stopped")
    else:
        print("not running")


def status_info() -> dict:
    info = {"pid": running_pid(), "socks5": None, "provider": None,
            "client_address": None, "log": str(logfile())}
    try:
        text = config_path().read_text()
        m = re.search(r"bind_address = '([^']+)'", text)
        if m:
            info["socks5"] = m.group(1)
        m = re.search(r"provider_mix_address = '([^']+)'", text)
        if m:
            info["provider"] = m.group(1)
    except OSError:
        pass
    try:
        log = _ANSI.sub("", logfile().read_text())
        m = re.findall(r"The address of this client is: (\S+)", log)
        if m:
            info["client_address"] = m[-1]
    except OSError:
        pass
    return info


def cmd_status() -> None:
    print(json.dumps(status_info(), indent=2))


# ---------------------------------------------------------------- rpc path

def mixnet_opener():
    """urllib opener whose connections go through the local nym socks5 client."""
    try:
        import socks
    except ImportError:
        sys.exit("PySocks is required: pip install pysocks")
    socks.setdefaultproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", SOCKS5_PORT)
    socket.socket = socks.socksocket  # noqa: process-local monkeypatch
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call_via_mixnet(method, params, timeout=90, _raw=None):
    """Single call (method, params) or raw passthrough of a full request body (batch).

    Retries across independent public upstreams; every attempt exits the mixnet.
    """
    opener = mixnet_opener()
    payload = _raw if _raw is not None else json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last_err = None
    for up in UPSTREAMS:
        try:
            req = urllib.request.Request(up, data=payload,
                                         headers={"Content-Type": "application/json"})
            return json.loads(opener.open(req, timeout=timeout).read())
        except Exception as e:
            last_err = e
    raise RuntimeError(f"all upstreams failed over mixnet: {last_err}")


class GatewayHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[gateway] " + fmt % args + "\n")

    def do_GET(self):
        out = b'{"solmix":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            parsed = json.loads(body)
            if isinstance(parsed, list):  # JSON-RPC batch: passthrough verbatim
                resp = call_via_mixnet(None, None, _raw=body)
                out = json.dumps(resp).encode()
            else:
                resp = call_via_mixnet(parsed.get("method"), parsed.get("params", []))
                resp["id"] = parsed.get("id")
                out = json.dumps(resp).encode()
        except Exception as e:
            out = json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32000, "message": str(e)}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def cmd_gateway(port: int) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), GatewayHandler)
    print(f"solmix gateway: Solana JSON-RPC at http://127.0.0.1:{port} -> Nym mixnet -> public RPCs")
    print(f"e.g.: solana --url http://127.0.0.1:{port} balance <addr>")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_ip() -> None:
    print(json.loads(mixnet_opener().open(IP_ECHO, timeout=90).read())["ip"])


COMMANDS = ("setup", "stop", "status", "ip", "gateway")


def main() -> None:
    # any first token that is not a builtin command is a one-shot RPC method
    if len(sys.argv) > 1 and sys.argv[1] not in COMMANDS and not sys.argv[1].startswith("-"):
        method, raw_params = sys.argv[1], sys.argv[2:]
        params = [json.loads(x) if x.startswith(("[", "{", '"')) or x in ("true", "false", "null")
                  else x for x in raw_params]
        print(json.dumps(call_via_mixnet(method, params), indent=2))
        return
    p = argparse.ArgumentParser(prog="solmix",
                                description="Solana JSON-RPC over the Nym mixnet. "
                                "Any first argument that is not a command is treated as a "
                                "one-shot JSON-RPC method: solmix getBalance <addr>")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("setup", help="init + start nym-socks5-client on a random exit gateway")
    sub.add_parser("stop", help="stop the background nym-socks5-client")
    sub.add_parser("status", help="show client state")
    sub.add_parser("ip", help="show the IP RPC providers see")
    g = sub.add_parser("gateway", help="serve local JSON-RPC endpoint")
    g.add_argument("port", nargs="?", type=int, default=DEFAULT_GATEWAY_PORT)
    args = p.parse_args()
    if args.cmd == "setup":
        cmd_setup()
    elif args.cmd == "stop":
        cmd_stop()
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "ip":
        cmd_ip()
    elif args.cmd == "gateway":
        cmd_gateway(args.port)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
