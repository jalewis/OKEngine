#!/usr/bin/env python3
"""budget-guard — engine spend cap / kill switch (okengine#35).

A `no_agent` cron (it never calls the LLM itself) that reads the agent runtime's
own token tally from the Hermes state DB, and when usage over a rolling window
crosses an operator-set budget it PAUSES the cost-bearing crons (every agent
job) via cron-plus — leaving the free `no_agent` maintenance scripts running, so
the vault stays healthy while ingest is throttled.

OKEngine has no other spend limit; this is opt-in and OFF unless a budget is set.

Env (all optional; with no budget set this exits 0 as a no-op):
  OKENGINE_BUDGET_TOKENS      int   — trip when window token usage >= this
  OKENGINE_BUDGET_USD         float — trip when window estimated cost >= this
  OKENGINE_BUDGET_PRICE_PER_MTOK float (default 0) — blended $/1M tokens, for the
                                  USD estimate (operator-supplied; we don't guess)
  OKENGINE_BUDGET_WINDOW      day|week|month (default day) — rolling window
  OKENGINE_BUDGET_RESUME      auto|manual (default auto) — auto re-enables when
                                  usage ages back under budget; manual stays paused
                                  until `framework budget --resume` (re-enables the
                                  paused crons + clears state)
  OKENGINE_STATE_DB           path (default $HERMES_HOME/state.db or /opt/data/state.db)
  OKENGINE_CRON_PLUS_JOBS     path (default /opt/data/cron-plus/jobs.json)
  OKENGINE_CRON_PLUS_CLI      path (default /opt/data/plugins/cron-plus/cli.py)
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

SELF_NAME = "budget-guard"
WINDOWS = {"day": 86400, "week": 604800, "month": 2592000}
_TOKEN_COLS = ("input_tokens", "output_tokens", "cache_read_tokens",
               "cache_write_tokens", "reasoning_tokens")


def window_seconds(name: str) -> int:
    return WINDOWS.get((name or "day").strip().lower(), WINDOWS["day"])


def _hermes_home() -> str:
    return os.environ.get("HERMES_HOME") or "/opt/data"


def _state_db_path() -> str:
    return os.environ.get("OKENGINE_STATE_DB") or str(Path(_hermes_home()) / "state.db")


def tokens_in_window(db_path: str | os.PathLike, window_s: int, now: float) -> int:
    """Sum all token columns for sessions started within the trailing window.
    Tolerates a missing DB / table / column (returns 0) so a fresh deploy is a
    clean no-op rather than an error."""
    p = Path(db_path)
    if not p.is_file():
        return 0
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
        if not have:
            return 0
        cols = [c for c in _TOKEN_COLS if c in have]
        if not cols:
            return 0
        tcol = "started_at" if "started_at" in have else None
        expr = " + ".join(f"COALESCE({c},0)" for c in cols)
        if tcol:
            cutoff = now - window_s
            row = con.execute(
                f"SELECT COALESCE(SUM({expr}),0) FROM sessions WHERE {tcol} >= ?",
                (cutoff,)).fetchone()
        else:
            row = con.execute(f"SELECT COALESCE(SUM({expr}),0) FROM sessions").fetchone()
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0
    finally:
        con.close()


def cost_bearing_ids(jobs: list, self_name: str = SELF_NAME) -> list[tuple[str, str]]:
    """(id, name) for every cost-bearing job to PAUSE when over budget — i.e. an ENABLED agent
    job (not no_agent), excluding the guard itself. The free no_agent maintenance scripts keep
    running.

    Skips a job that is ALREADY disabled: cron-plus pause is `enabled:False`, so capturing an
    operator-disabled job here would let auto-resume flip it back to enabled — silently reverting a
    deliberate maintenance pause (okengine#178). The guard must only pause (and later resume) what
    was actually running when it tripped."""
    out = []
    for j in jobs or []:
        if j.get("no_agent"):
            continue
        if not j.get("enabled", True):
            continue                       # already disabled (operator maintenance) — don't touch
        if j.get("name") == self_name:
            continue
        jid = j.get("id")
        if jid:
            out.append((jid, j.get("name") or jid))
    return out


def estimated_usd(tokens: int, price_per_mtok: float) -> float:
    return (tokens / 1_000_000.0) * price_per_mtok if price_per_mtok else 0.0


def decide(*, over_budget: bool, currently_paused: bool, resume_policy: str) -> str:
    """Pure decision: 'pause', 'resume', or 'noop'."""
    if over_budget and not currently_paused:
        return "pause"
    if not over_budget and currently_paused and resume_policy == "auto":
        return "resume"
    return "noop"


# ── effectful helpers ────────────────────────────────────────────────────────
def _state_path() -> Path:
    return Path(_hermes_home()) / "budget-guard-state.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        _state_path().write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        print(f"budget-guard: WARN could not write state: {e}", file=sys.stderr)


def _load_jobs() -> list:
    p = Path(os.environ.get("OKENGINE_CRON_PLUS_JOBS")
             or str(Path(_hermes_home()) / "cron-plus" / "jobs.json"))
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return d.get("jobs", d) if isinstance(d, dict) else (d if isinstance(d, list) else [])


def _cronplus(action: str, job_id: str) -> bool:
    """pause/resume one job via the cron-plus CLI. Best-effort: logs and returns
    False if cron-plus isn't reachable (the guard must never crash the tick)."""
    cli = os.environ.get("OKENGINE_CRON_PLUS_CLI") \
        or str(Path(_hermes_home()) / "plugins" / "cron-plus" / "cli.py")
    if not Path(cli).is_file():
        print(f"budget-guard: WARN cron-plus CLI not found at {cli}", file=sys.stderr)
        return False
    try:
        subprocess.run([sys.executable, cli, action, job_id],
                       check=True, capture_output=True, timeout=30)
        return True
    except (subprocess.SubprocessError, OSError) as e:
        print(f"budget-guard: WARN {action} {job_id} failed: {e}", file=sys.stderr)
        return False


def resume(reason: str = "manual") -> int:
    """Re-enable the cost-bearing crons a budget trip paused, and clear the pause state.

    Returns the number of crons re-enabled (0 if not currently paused). This is the
    manual recovery path behind `framework budget --resume` (the supported way back when
    OKENGINE_BUDGET_RESUME=manual); auto mode calls it from main() once window usage ages
    back under budget. Idempotent: a no-op when there is no active trip."""
    state = _load_state()
    if not state.get("paused"):
        print("budget-guard: not paused — nothing to resume.")
        return 0
    ids = state.get("paused_ids") or []
    resumed = [jid for jid in ids if _cronplus("resume", jid)]
    still = [jid for jid in ids if jid not in resumed]
    if not still:
        # every paused cron came back — safe to clear the tripped state
        _save_state({"paused": False, "resumed_at": time.time(),
                     "note": f"{reason}-resume", "resumed_count": len(resumed)})
        print(f"budget-guard: ✅ resumed {len(resumed)} cron(s) ({reason}).", file=sys.stderr)
    else:
        # some resume calls FAILED (cron-plus down / jobs.json lock / uid-desynced write). Do NOT
        # clear paused — that would strand those crons disabled while reporting "not paused". Keep
        # `paused` with only the STILL-disabled ids so the next tick re-attempts them (okengine
        # invariant-audit #14).
        _save_state({"paused": True, "paused_ids": still, "resumed_at": time.time(),
                     "note": f"{reason}-resume PARTIAL", "resumed_count": len(resumed)})
        print(f"budget-guard: ⚠ resume PARTIAL ({reason}) — {len(resumed)}/{len(ids)} resumed, "
              f"{len(still)} still disabled; retrying next tick.", file=sys.stderr)
    return len(resumed)


def reconcile_pause(state: dict, jobs: list) -> list[str]:
    """Re-apply the budget pause to any recorded cron a jobs.json redeploy silently re-enabled.

    The guard decides pause/resume from its own state file; it never re-derives whether the crons it
    paused are STILL disabled in the live cron-plus jobs.json. A wholesale `deploy-cron-plus-jobs.sh`
    redeploy overwrites jobs.json from the enabled:True source, re-enabling every cost-bearing cron
    while state still says paused — so decide() no-ops and the cap is silently defeated. Re-derive
    from the LIVE enabled-status and re-pause the drifted crons (okengine invariant-audit #13).

    Returns the ids actually re-paused. A no-op when not paused, no ids recorded, or nothing drifted.
    A job absent from jobs.json can't be paused (nothing to act on) — skip it."""
    if not state.get("paused"):
        return []
    ids = state.get("paused_ids") or []
    if not ids:
        return []
    live = {j.get("id"): j for j in jobs or [] if j.get("id")}
    drifted = [jid for jid in ids
               if (j := live.get(jid)) is not None and j.get("enabled", True)]
    return [jid for jid in drifted if _cronplus("pause", jid)]


def _parse_budget_env(name: str, cast) -> tuple[float, bool]:
    """Parse a numeric budget env var, FAILING CLOSED on a malformed value.

    Unset/empty -> (0, False): the disabled happy path (matches the old `or 0` read). A SET-but-
    malformed value (a typo, `$50`, `1,000,000` thousands separators) must NOT crash the guard or
    silently leave the deployment uncapped — the guard's whole reason for existing is the cap, and it
    would fail open exactly when the operator tried hardest to configure it. Warn loudly and return
    (0, True) so main() treats the deployment as over-budget and pauses (okengine invariant-audit
    #22)."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return 0, False
    try:
        return cast(raw), False
    except (TypeError, ValueError):
        print(f"budget-guard: WARN {name}={raw!r} is not a valid number — treating the deployment as "
              f"OVER BUDGET and pausing cost-bearing crons (fail-closed). Fix the value (a plain "
              f"number, no '$' or thousands separators).", file=sys.stderr)
        return 0, True


def main(argv: list[str] | None = None) -> int:
    tok_budget, tok_bad = _parse_budget_env("OKENGINE_BUDGET_TOKENS", int)
    usd_budget, usd_bad = _parse_budget_env("OKENGINE_BUDGET_USD", float)
    price, price_bad = _parse_budget_env("OKENGINE_BUDGET_PRICE_PER_MTOK", float)
    malformed = tok_bad or usd_bad or price_bad
    resume_policy = (os.environ.get("OKENGINE_BUDGET_RESUME") or "auto").strip().lower()
    win_name = os.environ.get("OKENGINE_BUDGET_WINDOW") or "day"

    # A malformed budget must still enforce (fail-closed) — check it BEFORE the no-budget no-op, or a
    # typo'd value (parsed to 0) would fall through here and run uncapped, the very bug this guards.
    if not malformed and not tok_budget and not usd_budget:
        print("budget-guard: no budget set (OKENGINE_BUDGET_TOKENS/_USD) — disabled, no-op")
        return 0
    # A USD cap needs a token->USD price. Without OKENGINE_BUDGET_PRICE_PER_MTOK the `usd_budget
    # and price` term below is always False, so the USD cap silently NEVER trips (fail-open — the
    # operator thinks they're capped and aren't). Surface it loudly (okengine#178).
    if usd_budget and not price:
        print("budget-guard: WARN OKENGINE_BUDGET_USD is set but OKENGINE_BUDGET_PRICE_PER_MTOK "
              "is unset — the USD cap is INERT (no token->USD conversion) and will NEVER trip. "
              "Set the price, or cap with OKENGINE_BUDGET_TOKENS instead.", file=sys.stderr)

    now = time.time()
    win_s = window_seconds(win_name)
    tokens = tokens_in_window(_state_db_path(), win_s, now)
    usd = estimated_usd(tokens, price)
    over = malformed or bool((tok_budget and tokens >= tok_budget)
                             or (usd_budget and price and usd >= usd_budget))

    state = _load_state()
    paused = bool(state.get("paused"))
    action = decide(over_budget=over, currently_paused=paused, resume_policy=resume_policy)

    usage_str = f"{tokens:,} tok/{win_name}" + (f" (~${usd:,.2f})" if price else "")
    budget_str = (f"{tok_budget:,} tok" if tok_budget else "") + \
                 (f" / ${usd_budget:,.2f}" if usd_budget else "")
    print(f"budget-guard: usage {usage_str}  budget {budget_str}  "
          f"over={over} paused={paused} -> {action}")

    if action == "pause":
        ids = cost_bearing_ids(_load_jobs())
        # pause each cost-bearing cron; track which ACTUALLY paused. `paused` is True only when the
        # cap is genuinely enforced (every cost-bearing cron paused, or there were none). If some/all
        # pauses failed (cron-plus down, jobs.json unreadable/corrupt -> ids==[], lock race), leaving
        # paused=True would make decide() no-op forever while crons keep spending past the cap — a
        # fail-OPEN circuit breaker. Keeping paused=False lets the next tick RE-attempt (idempotent)
        # until the cap actually holds (okengine invariant-audit #3).
        paused = [(jid, name) for jid, name in ids if _cronplus("pause", jid)]
        fully = len(paused) == len(ids)
        state = {"paused": fully, "paused_ids": [jid for jid, _ in paused],
                 "paused_names": [name for _, name in paused], "tripped_at": now, "window": win_name,
                 "usage_tokens": tokens, "reason": f"over budget ({usage_str} >= {budget_str})"}
        _save_state(state)
        if fully:
            print(f"budget-guard: ⛔ BUDGET TRIPPED — paused {len(paused)} cost-bearing cron(s): "
                  f"{', '.join(n for _, n in paused)}. Free maintenance crons keep running. "
                  f"Resume policy: {resume_policy}.", file=sys.stderr)
        else:
            print(f"budget-guard: ⚠ OVER BUDGET but pause INCOMPLETE — {len(paused)}/{len(ids)} "
                  f"cost-bearing cron(s) paused (cron-plus/jobs.json failure?). Cap NOT yet enforced; "
                  f"retrying next tick.", file=sys.stderr)
    elif action == "resume":
        resume("auto")
    elif paused:
        # noop by the budget decision, but we BELIEVE we're paused — reconcile against the live
        # jobs.json in case a redeploy silently re-enabled the crons we paused (okengine
        # invariant-audit #13). Without this the cap is defeated by any jobs.json redeploy.
        repaused = reconcile_pause(state, _load_jobs())
        if repaused:
            print(f"budget-guard: ⚠ RECONCILE — {len(repaused)} cost-bearing cron(s) were re-enabled "
                  f"(jobs.json redeploy?) while the budget pause is active; re-paused: "
                  f"{', '.join(repaused)}. The kill-switch holds.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
