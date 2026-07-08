# shellcheck shell=bash
# pack_data.sh — enumerate the pack's domain data tables to stage into /opt/data/config/.
#
# The pack contract (docs/deploy-a-new-domain.md) is `data/*  # domain data tables (consumed by
# engine-template crons)` — the WHOLE data/ tree, not a curated allowlist. A hardcoded set of
# filenames silently drops any table a future pack adds (okengine invariant-audit #9): the deploy
# reports success, then the domain cron that opens /opt/data/config/<its-table> raises
# FileNotFoundError at the scheduled tick — a silent no-op lane with no warning at deploy time.
#
# We enumerate every regular file at the top of data/ and skip only `.gitkeep`, the empty
# placeholder a data-less pack ships to keep the dir under git. Basenames are emitted one per line
# (relative to data/, so the caller tars from within PACK_DATA and preserves the flat layout the
# crons expect at /opt/data/config/<name>).

enumerate_pack_data_files() {   # $1=pack_data_dir  -> stdout: one basename per data file, sorted
    local pack_data="$1"
    [ -d "$pack_data" ] || return 0
    find "$pack_data" -maxdepth 1 -type f ! -name '.gitkeep' -printf '%f\n' | LC_ALL=C sort
}
