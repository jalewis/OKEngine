#!/usr/bin/env python3
"""Docker Hub reachability canary (okengine#751).

Docker Hub serves image blobs from one of two CDNs, chosen per request. When this network's DNS
filter blocked cloudfront.net, pulls failed only when a request happened to be routed to
CloudFront -- so the breakage was intermittent, a single successful pull proved nothing, and it
went undiagnosed for weeks (okengine#556). A pull-based canary inherits that exact lottery: it
passes whenever it lands on the working CDN. This one does not pull. It checks every host a pull
can touch, BY NAME, on every run.

For each host it checks three things, because each defeats a different kind of filter:

  1. an IPv4 address comes back          -- a filter that answers NXDOMAIN (what #751 was)
  2. every address is globally routable  -- a filter that "sinkholes" to 0.0.0.0, loopback or a
                                            LAN block page; a bare "did it resolve" check passes
                                            those, and they are the default in common filters
  3. TLS completes against that address
     with the REAL hostname verified     -- a block page served from a public IP, or anything
                                            else answering on 443 that is not the CDN

It first checks WHICH resolver it is using. A CI job runs in a container, and the runner's image
pulls resolve through the host. If this container were handed public DNS (8.8.8.8 and friends),
it would bypass the very filter it exists to watch and report a pass that proves nothing. That
case is UNDETECTABLE, never a pass.

Exit codes: 0 every host reachable; 1 a host failed; 2 undetectable (wrong resolver). A failure
outranks undetectable -- a host failing through public DNS is still a real outage.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import ssl
import sys
from collections.abc import Callable
from pathlib import Path

#: Every host a Docker Hub pull touches. Both CDNs are listed on purpose: naming only the one that
#: works is how this network's docs spent weeks pointing at the wrong hostname.
HOSTS = (
    "registry-1.docker.io",
    "auth.docker.io",
    "production.cloudflare.docker.com",
    "production.cloudfront.docker.com",
)

#: Public resolvers. If the container is using one, it is not asking the resolver the runner's
#: pulls go through, so a filter on that resolver is invisible from here.
PUBLIC_RESOLVERS = frozenset({
    "8.8.8.8", "8.8.4.4",                      # Google
    "1.1.1.1", "1.0.0.1",                      # Cloudflare
    "9.9.9.9", "149.112.112.112",              # Quad9
    "208.67.222.222", "208.67.220.220",        # OpenDNS
    "2001:4860:4860::8888", "2001:4860:4860::8844",
    "2606:4700:4700::1111", "2606:4700:4700::1001",
})

TIMEOUT_SECONDS = 10.0

OK, FAILED, UNDETECTABLE = 0, 1, 2


def nameservers(resolv_conf: str) -> list[str]:
    """The `nameserver` entries of a resolv.conf, in order, comments ignored."""
    servers = []
    for line in resolv_conf.splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) >= 2 and fields[0] == "nameserver":
            servers.append(fields[1])
    return servers


def resolver_verdict(servers: list[str]) -> tuple[bool, str]:
    """Whether a result from these resolvers can be trusted to reflect the runner's own DNS."""
    if not servers:
        return False, "no nameserver found in resolv.conf; cannot tell which resolver answered"
    public = [server for server in servers if server in PUBLIC_RESOLVERS]
    if public:
        return False, (f"this container resolves through public DNS {public}. A filter on the "
                       "runner's own resolvers would be bypassed, so a pass here would prove nothing")
    return True, f"resolving through {servers}"


def resolve_ipv4(host: str) -> list[str]:
    """Every IPv4 address `host` resolves to. Raises OSError (gaierror) when there is none."""
    infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def tls_handshake(address: str, host: str, *,
                  connect: Callable[..., socket.socket] = socket.create_connection,
                  context_factory: Callable[[], ssl.SSLContext] = ssl.create_default_context) -> str:
    """Complete TLS with `address`, verifying the certificate is valid for `host`.

    Connecting to the ADDRESS but verifying against the HOSTNAME is the point: it proves the thing
    answering on the address we resolved is really that host. `server_hostname` is what makes the
    default context check the name; drop it and a block page with any valid certificate would pass.
    """
    context = context_factory()
    with connect((address, 443), timeout=TIMEOUT_SECONDS) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            return tls.version() or "unknown"


def probe(host: str, *, resolve: Callable[[str], list[str]],
          handshake: Callable[[str, str], str]) -> dict:
    """Check one host through all three layers, stopping at the first that fails."""
    try:
        addresses = resolve(host)
    except OSError as exc:
        return {"host": host, "ok": False, "failure": "no-address", "detail": str(exc)}
    if not addresses:
        return {"host": host, "ok": False, "failure": "no-address", "detail": "resolved to nothing"}

    sinkholed = [address for address in addresses if not ipaddress.ip_address(address).is_global]
    if sinkholed:
        return {"host": host, "ok": False, "failure": "sinkholed", "addresses": addresses,
                "detail": f"not globally routable: {sinkholed}"}

    try:
        version = handshake(addresses[0], host)
    except OSError as exc:              # ssl.SSLError and socket timeouts are both OSError
        return {"host": host, "ok": False, "failure": "tls", "addresses": addresses,
                "detail": f"{type(exc).__name__}: {exc}"}
    return {"host": host, "ok": True, "addresses": addresses, "tls": version}


def run(resolv_conf: str, *, resolve: Callable[[str], list[str]],
        handshake: Callable[[str, str], str], hosts: tuple[str, ...] = HOSTS) -> tuple[int, dict]:
    servers = nameservers(resolv_conf)
    trusted, resolver_detail = resolver_verdict(servers)
    results = [probe(host, resolve=resolve, handshake=handshake) for host in hosts]

    if not all(result["ok"] for result in results):
        code = FAILED
    elif not trusted:
        code = UNDETECTABLE
    else:
        code = OK
    return code, {"code": code, "resolvers": servers, "resolver_trusted": trusted,
                  "resolver_detail": resolver_detail, "hosts": results}


def render(report: dict) -> str:
    lines = [f"registry-canary: {report['resolver_detail']}"]
    for result in report["hosts"]:
        if result["ok"]:
            lines.append(f"registry-canary:   ok     {result['host']:<36} "
                         f"{','.join(result['addresses'])} ({result['tls']})")
        else:
            lines.append(f"registry-canary:   FAILED {result['host']:<36} "
                         f"[{result['failure']}] {result['detail']}")
    verdict = {OK: "PASS -- every host a Docker Hub pull touches is reachable",
               FAILED: "FAIL -- a Docker Hub host is unreachable; image pulls that land on it will fail",
               UNDETECTABLE: "UNDETECTABLE -- not a pass; see the resolver line above"}[report["code"]]
    lines.append(f"registry-canary: {verdict}")
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, resolve: Callable[[str], list[str]] = resolve_ipv4,
         handshake: Callable[[str, str], str] = tls_handshake) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--resolv-conf", type=Path, default=Path("/etc/resolv.conf"))
    parser.add_argument("--json", type=Path, help="also write the report here")
    args = parser.parse_args(argv)

    try:
        resolv_conf = args.resolv_conf.read_text(encoding="utf-8")
    except OSError:
        resolv_conf = ""               # reported as "no nameserver found" -> undetectable

    code, report = run(resolv_conf, resolve=resolve, handshake=handshake)
    print(render(report))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
