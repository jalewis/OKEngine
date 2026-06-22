#!/usr/bin/env bash
# Render skeleton/ into a new okpack-<domain> pack, substituting every {{TOKEN}}.
# See PLACEHOLDERS.md for the token reference and README.md for usage.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SKELETON="$SCRIPT_DIR/skeleton"

usage() {
  cat >&2 <<EOF
usage: new-pack.sh <pack-name> [title] [options]

  <pack-name>        e.g. okpack-fin   (convention: okpack-<domain>)
  [title]            human title, e.g. "finance threat & fraud vault"

options:
  --offset N         host-port offset (reader=9200+N, mcp=8730+N)   [default 0]
  --engine TAG       engine pin                                     [default v0.2.0]
  --hermes-pin TAG   Hermes runtime pin (engine.version)            [default v2026.6.5]
  --brief-hour H     UTC hour (0-23) for the daily brief            [default 13]
  --owner NAME       GitHub owner for the README CI badge           [default REPLACE_OWNER]
  --license NAME     LICENSE to ship: apache-2.0 | none             [default apache-2.0]
  --blurb TEXT       one-line description (README + GitHub About)
  --out DIR          output directory                              [default ./<pack-name> in CWD]
EOF
  exit 2
}

PACK="" TITLE="" OFFSET=0 ENGINE="v0.2.0" HERMES_PIN="v2026.6.5" BRIEF_HOUR=13 OWNER="REPLACE_OWNER" LICENSE="apache-2.0" BLURB="" OUT=""
POSITIONAL=()
while [ $# -gt 0 ]; do
  case "$1" in
    --offset)     OFFSET="$2"; shift 2 ;;
    --engine)     ENGINE="$2"; shift 2 ;;
    --hermes-pin) HERMES_PIN="$2"; shift 2 ;;
    --brief-hour) BRIEF_HOUR="$2"; shift 2 ;;
    --owner)      OWNER="$2"; shift 2 ;;
    --license)    LICENSE="$2"; shift 2 ;;
    --blurb)      BLURB="$2"; shift 2 ;;
    --out)        OUT="$2"; shift 2 ;;
    -h|--help)    usage ;;
    -*)           echo "unknown option: $1" >&2; usage ;;
    *)            POSITIONAL+=("$1"); shift ;;
  esac
done
[ "${#POSITIONAL[@]}" -ge 1 ] || usage
PACK="${POSITIONAL[0]}"
TITLE="${POSITIONAL[1]:-}"

[ -d "$SKELETON" ] || { echo "error: skeleton/ not found next to this script" >&2; exit 1; }
case "$PACK" in okpack-*) ;; *) echo "warn: convention is okpack-<domain> (got '$PACK')" >&2 ;; esac
case "$OFFSET" in *[!0-9]*) echo "error: --offset must be an integer" >&2; exit 2 ;; esac

DOMAIN="${PACK#okpack-}"
[ -n "$TITLE" ] || TITLE="$DOMAIN knowledge vault"
[ -n "$BLURB" ] || BLURB="Agent-curated $TITLE for the OKEngine framework — ingests open feeds into a compounding, cross-linked knowledge graph."
ENV_PREFIX=$(printf '%s' "$PACK" | tr 'a-z-' 'A-Z_')
PACK_UNDERSCORE=$(printf '%s' "$PACK" | tr '-' '_')
READER_PORT=$((9200 + OFFSET))
MCP_PORT=$((8730 + OFFSET))
LICENSE_YEAR=$(date +%Y)
case "$LICENSE" in apache-2.0|none) ;; *) echo "error: --license must be 'apache-2.0' or 'none'" >&2; exit 2 ;; esac
mint_id() { openssl rand -hex 6 2>/dev/null || python3 -c "import secrets;print(secrets.token_hex(6))"; }
CRON_ID_1=$(mint_id); CRON_ID_2=$(mint_id)
OUT="${OUT:-$PWD/$PACK}"
# refuse to render inside the template's own dir/git tree (would commit a pack into it)
out_parent=$(cd "$(dirname "$OUT")" 2>/dev/null && pwd || true)
case "${out_parent:-/}/" in
  "$SCRIPT_DIR"/*) echo "error: refusing to render inside the template dir ($SCRIPT_DIR); cd elsewhere or pass --out" >&2; exit 1 ;;
esac

if [ -e "$OUT" ] && [ -n "$(ls -A "$OUT" 2>/dev/null || true)" ]; then
  echo "error: output dir '$OUT' exists and is not empty" >&2; exit 1
fi

mkdir -p "$OUT"
cp -R "$SKELETON/." "$OUT/"

# rename any templated filenames (e.g. {{PACK_UNDERSCORE}}_feed_fetch.py)
while IFS= read -r f; do
  nf=${f//\{\{PACK_UNDERSCORE\}\}/$PACK_UNDERSCORE}
  [ "$f" = "$nf" ] || mv "$f" "$nf"
done < <(find "$OUT" -depth -name '*{{PACK_UNDERSCORE}}*')

# license: drop the LICENSE file if the operator opted out
[ "$LICENSE" = "none" ] && rm -f "$OUT/LICENSE"

# substitute tokens in every text file
export PACK DOMAIN TITLE BLURB ENGINE HERMES_PIN BRIEF_HOUR OWNER LICENSE_YEAR ENV_PREFIX PACK_UNDERSCORE \
       READER_PORT MCP_PORT CRON_ID_1 CRON_ID_2 OFFSET
python3 - "$OUT" <<'PY'
import os, sys
root = sys.argv[1]
e = os.environ
repl = {
    "{{PACK}}": e["PACK"], "{{DOMAIN}}": e["DOMAIN"], "{{TITLE}}": e["TITLE"],
    "{{BLURB}}": e["BLURB"], "{{ENGINE_VERSION}}": e["ENGINE"], "{{HERMES_PIN}}": e["HERMES_PIN"],
    "{{PORT_OFFSET}}": e["OFFSET"], "{{READER_PORT}}": e["READER_PORT"], "{{MCP_PORT}}": e["MCP_PORT"],
    "{{ENV_PREFIX}}": e["ENV_PREFIX"], "{{PACK_UNDERSCORE}}": e["PACK_UNDERSCORE"],
    "{{BRIEF_HOUR}}": e["BRIEF_HOUR"], "{{OWNER}}": e["OWNER"], "{{LICENSE_YEAR}}": e["LICENSE_YEAR"],
    "{{CRON_ID_1}}": e["CRON_ID_1"], "{{CRON_ID_2}}": e["CRON_ID_2"],
}
for dp, _, fs in os.walk(root):
    for fn in fs:
        p = os.path.join(dp, fn)
        try:
            s = open(p, encoding="utf-8").read()
        except (UnicodeDecodeError, IsADirectoryError):
            continue
        n = s
        for k, v in repl.items():
            n = n.replace(k, v)
        if n != s:
            open(p, "w", encoding="utf-8").write(n)
# fail loudly if any template token survived (match {{UPPER_SNAKE}} only, so we
# don't trip over escaped braces in code, e.g. Python f-strings' {{...}}).
import re
TOKEN = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")
leftover = []
for dp, _, fs in os.walk(root):
    for fn in fs:
        p = os.path.join(dp, fn)
        try:
            if TOKEN.search(open(p, encoding="utf-8").read()):
                leftover.append(os.path.relpath(p, root))
        except (UnicodeDecodeError, IsADirectoryError):
            pass
if leftover:
    sys.exit("error: unsubstituted tokens remain in: " + ", ".join(sorted(leftover)))
PY

echo "rendered $PACK -> $OUT"
echo "  reader $READER_PORT  mcp $MCP_PORT  engine $ENGINE"
echo
echo "next:"
echo "  1. edit $OUT/schema.yaml   (replace the example DOMAIN ENTITY types)"
echo "  2. edit $OUT/CLAUDE.md      (resolve the TODO: markers)"
echo "  3. edit $OUT/feeds/feeds.opml.example (replace the EXAMPLE feeds; feeds.opml stays empty until you opt in)"
echo "  4. cd $OUT && python3 validate.py"
echo
echo "ships inert: no active sources, crons disabled. Enable per $OUT/README.md."
