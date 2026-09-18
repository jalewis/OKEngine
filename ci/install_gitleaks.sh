#!/bin/sh
set -eu

# Keep the blocking secret gate independent of GitHub on the normal path. GitLab restores this
# version-keyed tarball cache before the job; a cold cache still downloads fail-closed. Verify the
# upstream release digest on EVERY run so a mutable/shared runner cache is never executable trust.
version=${GITLEAKS_VERSION:?GITLEAKS_VERSION is required}
expected=${GITLEAKS_SHA256:?GITLEAKS_SHA256 is required}
cache_root=${GITLEAKS_CACHE_ROOT:-.cache/ci-tools/gitleaks}
archive="$cache_root/gitleaks_${version}_linux_x64.tar.gz"
tool_root=${GITLEAKS_TOOL_ROOT:-artifacts/ci-tools/gitleaks-$version}
tool="$tool_root/gitleaks"
curl_bin=${CURL_BIN:-curl}

mkdir -p "$cache_root" "$tool_root"

valid_archive() {
    [ -f "$archive" ] && printf '%s  %s\n' "$expected" "$archive" | sha256sum -c - >/dev/null 2>&1
}

if ! valid_archive; then
    rm -f "$archive"
    tmp=$(mktemp "$cache_root/.gitleaks-${version}.XXXXXX")
    trap 'rm -f "$tmp"' EXIT HUP INT TERM
    "$curl_bin" --fail --silent --show-error --location \
        --retry 5 --retry-all-errors --retry-delay 2 --connect-timeout 15 --max-time 300 \
        --output "$tmp" \
        "https://github.com/gitleaks/gitleaks/releases/download/v${version}/gitleaks_${version}_linux_x64.tar.gz"
    printf '%s  %s\n' "$expected" "$tmp" | sha256sum -c - >/dev/null
    mv "$tmp" "$archive"
    trap - EXIT HUP INT TERM
    echo "gitleaks: populated verified version $version cache" >&2
else
    echo "gitleaks: using verified version $version cache" >&2
fi

rm -f "$tool"
tar -xzf "$archive" -C "$tool_root" gitleaks
chmod 0755 "$tool"
printf '%s\n' "$tool"
