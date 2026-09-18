#!/usr/bin/env bash
# Pull the release stack's base images BEFORE the build, with bounded retries.
#
# Why this exists. Docker Hub redirects blob fetches to ONE OF TWO CDNs, chosen per request:
#
#     production.cloudflare.docker.com   dual-stack
#     production.cloudfront.docker.com   the one that failed here
#
# So the SAME pull succeeds when it lands on Cloudflare and fails when it lands on CloudFront, which
# is why this read as intermittent flake for weeks (okengine#556). It fails at the base-image
# metadata step, before a line of project code runs, and shows in the UI as a test failure.
#
# CORRECTED 2026-09-13 (okengine#751). This header previously said CloudFront "publishes NO A
# record" and is "IPv6-ONLY", and that the fix needed IPv6 egress or a different resolver. That was
# wrong. Measured from the runner host on 2026-09-13, production.cloudfront.docker.com resolves to
# an IPv4 address. The A record had been withheld by a DNS FILTER on this network, which blocked
# cloudfront.net -- the domain the cloudfront.com name resolves through. The observed pattern (AAAA
# answered, A missing, no IPv6 route here) is what such a filter produces, and it reproduced every
# symptom above. Removing the filter fixed it; nothing about IPv6 needed to change.
#
# The 2026-08-22 experiments still stand as measurements -- pinning Hub hostnames in /etc/hosts,
# disabling IPv6 in a privileged dind, and a glibc skopeo bypass all failed -- but they failed
# because the name was being filtered, not because the CDN lacked IPv4. They are why
# `services_privileged = true` was never the fix.
#
# Release CI now pulls its base images from the project registry instead (OKENGINE_*_BASE_IMAGE),
# which serves them from its own storage and never touches this CDN. This script still matters for
# public builds, and it keeps any failure ATTRIBUTABLE: the retry survives a transient redirect, and
# when every attempt fails, diagnose_environment() MEASURES whether a required host has an A record
# and says what it found, instead of asserting a cause. A withheld A record looks the same whether
# a filter or the upstream is responsible, which is exactly why this measures rather than guesses.
# The old message asserted IPv6 on any pull failure -- it would have been just as confident about
# an expired token or a rate limit.
#
# Retrying a NETWORK FETCH is legitimate; retrying a TEST until it passes is not. This retries only
# the pull, and if every attempt fails it exits non-zero naming the cause -- it never lets an
# unfetched image look like a passing gate.
set -uo pipefail

ATTEMPTS="${PREPULL_ATTEMPTS:-5}"
SLEEP="${PREPULL_SLEEP:-3}"
# Overridable so the diagnosis itself is testable against a simulated IPv6-only host; a test that
# can only run on the real network is a test that never runs in CI.
GETENT="${PREPULL_GETENT:-getent}"
IPCMD="${PREPULL_IP:-ip}"

# The hosts a Docker Hub pull actually touches. The two CDNs are BOTH listed on purpose: naming
# only the one that works is how the previous header sent readers after the wrong hostname.
HUB_HOSTS="registry-1.docker.io auth.docker.io production.cloudflare.docker.com production.cloudfront.docker.com"

diagnose_environment() {
    # MEASURE, never assert. Prints the address families each host resolves to and whether this
    # box has an IPv6 default route, then draws a conclusion ONLY when the facts support one.
    echo "prepull: --- environment diagnosis (measured, not assumed) ---" >&2
    # The actionable fact is whether a host has an A RECORD. A host without one is unreachable
    # from an IPv4-only network whether or not we can observe its AAAA -- and we often cannot:
    # glibc's ahostsv6 returns IPv4-MAPPED addresses (::ffff:1.2.3.4) and may omit real v6 entirely
    # on a box with no v6 route, so "AAAA=none" is reported as evidence, never relied on.
    no_a=""
    for host in $HUB_HOSTS; do
        a=$("$GETENT" ahostsv4 "$host" 2>/dev/null | awk '{print $1; exit}')
        aaaa=$("$GETENT" ahostsv6 "$host" 2>/dev/null | awk '$1 !~ /^::ffff:/ {print $1; exit}')
        printf 'prepull:   %-34s A=%-16s AAAA=%s\n' "$host" "${a:-NONE}" "${aaaa:-none/unobservable}" >&2
        [ -z "$a" ] && no_a="$no_a $host"
    done

    if command -v "$IPCMD" >/dev/null 2>&1; then
        if "$IPCMD" -6 route show default 2>/dev/null | grep -q .; then route6=yes; else route6=no; fi
    else
        route6=undetectable       # absent `ip` is not "no route" -- say so rather than concluding
    fi
    echo "prepull:   IPv6 default route: $route6" >&2
    # WHICH resolver answered is part of the evidence, not a detail. Measured 2026-08-22: the
    # okengine#556 fix was applied to one LAN resolver while the runner queried a different one,
    # so the name still failed and the CI error was the only place the nameserver appeared.
    # Naming it here means the next reader fixes the resolver that is actually being used.
    nameservers=$(awk '/^nameserver/ {printf "%s ", $2}' "${PREPULL_RESOLV:-/etc/resolv.conf}" 2>/dev/null)
    echo "prepull:   resolver(s) in use: ${nameservers:-undetectable}" >&2

    if [ -z "$no_a" ]; then
        echo "prepull: DIAGNOSIS — every host above has an IPv4 address, so okengine#556 (a" >&2
        echo "prepull: withheld CloudFront A record) is NOT the cause. Look elsewhere: credentials," >&2
        echo "prepull: rate limiting, a moved digest, or the daemon itself." >&2
    elif [ "$route6" = yes ]; then
        echo "prepull: DIAGNOSIS —${no_a} has no A record, but this host HAS an IPv6 route, so it" >&2
        echo "prepull: should still be reachable. okengine#556 is unlikely; look at the error above." >&2
    else
        echo "prepull: DIAGNOSIS —${no_a} has NO A RECORD and this host's IPv6 route is '$route6'," >&2
        echo "prepull: so the fetch cannot succeed. This is an ENVIRONMENT failure, not a test" >&2
        echo "prepull: result. Fix the resolver NAMED ABOVE so the alias returns IPv4 (fixing a" >&2
        echo "prepull: different resolver on the same LAN will not help), or give the runner IPv6." >&2
        echo "prepull: A DNS filter blocking cloudfront.net produces exactly this, and was the cause" >&2
        echo "prepull: last time (okengine#751) -- check the filter before assuming IPv6-only." >&2
        echo "prepull: See okengine#556 — this is the exact condition it documents." >&2
    fi
}

# Read the digests from the Dockerfiles themselves. A hard-coded copy here would be a second source
# of truth that silently desyncs the day someone bumps the pin.
DOCKERFILES=(
  okengine-reader/Dockerfile
  okengine-cockpit/Dockerfile
  okengine-mcp/Dockerfile
  okengine-mcp/Dockerfile.review
  okengine-operations/Dockerfile
  tests/e2e/smoke/Dockerfile.fault-gateway
)

# Each base image comes from ONE of two places. CI sets OKENGINE_<NAME>_BASE_IMAGE to the project
# registry's mirror (digest-pinned; the tag is only a readable path). Without it -- a public or
# deployment build -- the default is read from the Dockerfiles' own ARG lines rather than copied
# here, so there is no second source of truth to desync when a pin is bumped.
#
# The override REPLACES the Dockerfile default for that image; it does not add to it. Pulling both
# would fetch the public image CI is specifically avoiding.
base_images() {       # $1 = override value (may be empty), $2 = the Dockerfile ARG name
  if [ -n "$1" ]; then
    printf '%s\n' "$1"
  else
    grep -hE "^ARG +$2=" "${DOCKERFILES[@]}" 2>/dev/null | cut -d= -f2- | sort -u
  fi
}
mapfile -t IMAGES < <({
  base_images "${OKENGINE_PYTHON_BASE_IMAGE:-}" PYTHON_BASE_IMAGE
  base_images "${OKENGINE_NODE_BASE_IMAGE:-}" NODE_BASE_IMAGE
} | awk 'NF && !seen[$0]++')

if [ "${#IMAGES[@]}" -eq 0 ]; then
  echo "prepull: FAILED — no FROM lines found; refusing to report a pull that never happened" >&2
  exit 1
fi

for image in "${IMAGES[@]}"; do
  if [[ "$image" != *@sha256:* ]]; then
    echo "prepull: FAILED — base image is not digest-pinned: $image" >&2
    exit 1
  fi
done

rc=0
for image in "${IMAGES[@]}"; do
  ok=0
  for i in $(seq 1 "$ATTEMPTS"); do
    if docker pull --quiet "$image" >/dev/null 2>&1; then
      echo "prepull: ok (attempt $i/$ATTEMPTS) $image"
      ok=1
      break
    fi
    echo "prepull: attempt $i/$ATTEMPTS failed for $image" >&2
    sleep "$SLEEP"
  done
  if [ "$ok" -eq 0 ]; then
    echo "prepull: FAILED after $ATTEMPTS attempts: $image" >&2
    echo "prepull: last error follows, then a measured diagnosis (see this script's header)." >&2
    docker pull "$image" >&2 || true
    rc=1
  fi
done

[ "$rc" -ne 0 ] && diagnose_environment
exit "$rc"
