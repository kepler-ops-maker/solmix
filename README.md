# solmix

**Solana JSON-RPC through the Nym mixnet.** A local gateway + CLI that puts
all of your Solana RPC traffic on the mixnet, so RPC providers and network
observers never learn your IP address.

```
 solmix ──SOCKS5──> nym-socks5-client ──Sphinx packets──> entry gateway ──> 3 mix layers ──> exit gateway
 (your machine)     (your machine)       + cover traffic                                    network requester
                                                                                                    │ TCP (TLS inside)
                                                                                                    ▼
                                                              api.mainnet-beta.solana.com / publicnode / ankr / ...
```

## Why

Every Solana wallet, bot and CLI talks to an RPC provider, and every one of
those calls reveals *"this IP address asked about this address / sent this
transaction at this time"*. That one log line links your network identity to
your accounts, your balances and your transaction timing. Over weeks it is a
complete picture of your on-chain life, and it is collected by default.

solmix routes the same JSON-RPC calls through the Nym mixnet instead:

1. `solmix setup` asks the Nym API for exit gateways that are in the active
   set, run a network requester and apply the Nym exit policy, picks one
   **at random** (performance >= 0.95), and initialises Nym's official
   `nym-socks5-client` against it. Reply SURBs are on, so the exit does not
   even learn your mixnet address.
2. Every request opens a fresh SOCKS5 stream. The Nym client chops it into
   Sphinx packets with cover traffic and Poisson delays, and the exit
   gateway's network requester opens the TCP connection to the RPC provider.
3. **TLS is end to end** between solmix and the RPC provider. The exit sees
   only the destination host:port and ciphertext; certificates are checked
   against the real hostname.
4. **DNS is resolved by the exit.** Your local resolver never sees which RPC
   provider you use.

## Install

```
pip install pysocks            # the only Python dependency
# download Nym's official socks5 client and verify it:
#   https://github.com/nymtech/nym/releases  (nym-binaries-* release)
#   check sha256 against the release's hashes.json
chmod +x nym-socks5-client && mv nym-socks5-client ~/.solmix/
chmod +x solmix && ln -sf "$PWD/solmix" ~/.local/bin/solmix   # optional
```

No accounts, no API keys, no tracking. Python 3.10+, stdlib + PySocks.

## Use

```
solmix setup                 # pick a random exit gateway, init + start the mixnet client
solmix ip                    # show the IP RPC providers see (a Nym exit, not you)
solmix getHealth             # one-shot JSON-RPC call through the mixnet
solmix getBalance <address>  # any method, params as arguments
solmix gateway               # serve JSON-RPC at http://127.0.0.1:8899
solmix stop                  # stop the background mixnet client
```

Point any Solana wallet or CLI at the local gateway and everything it does
goes over the mixnet:

```
solana --url http://127.0.0.1:8899 balance <address>
```

Multiple independent public upstreams are tried per request
(api.mainnet-beta.solana.com, solana-rpc.publicnode.com, rpc.ankr.com); every
attempt exits the mixnet. JSON-RPC batches pass through verbatim.

## What it does not do

- The Nym API query in `setup` (like `nym-socks5-client`'s own topology
  fetch) goes direct: it reveals that you use Nym, nothing about your calls.
- The mixnet adds latency (seconds, not milliseconds). Use it for wallet
  operations, bots and queries that should not be linked to you - not for
  latency-critical trading loops.
- It protects the RPC path. Transactions you send are still public on-chain;
  solmix only breaks the IP-to-transaction link at broadcast/query time.

## Development

```
python3 test_solmix.py       # unit tests (mock RPC + fake socks5, no mixnet needed)
```

## License

MIT. The mixnet itself is Nym's official release binary; solmix is a thin
gateway/CLI around it.
