#!/usr/bin/env python3
"""Run and score OKEngine's bounded Cosmic Ray mutation scope.

The manifest is an explicit review boundary: a changed production module must
either have a targeted test command or the pre-merge gate fails.  Scheduled
runs execute every manifest entry.  Infrastructure failures and untriaged
survivors are compliance failures, never silently counted as killed mutants.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import fnmatch
import hashlib
import io
import json
import math
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tokenize
import urllib.error
import urllib.request
from contextlib import ExitStack, contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET


PRODUCTION_ROOTS = (
    "src/", "scripts/", "tools/", "okengine-mcp/", "okengine-reader/",
    "okengine-cockpit/", "okengine-projection/", "okengine-operations/",
    "extensions/", "plugins/", "migrations/",
)
INFRA_OUTCOMES = {None, "incompetent"}
TIMEOUT_SENTINEL = "OKENGINE_MUTATION_TIMEOUT"
PROGRESS_PREFIX = "mutation-progress "


def emit_progress(event: str, **fields) -> None:
    """Emit one deterministic, immediately-visible mutation progress record."""
    payload = {"event": event, **fields}
    print(PROGRESS_PREFIX + json.dumps(payload, sort_keys=True, separators=(",", ":")),
          file=sys.stderr, flush=True)


@contextmanager
def target_heartbeats(path: str, started: float, interval_seconds: float = 60.0):
    """Report liveness while any phase of one target is active without changing its timeout."""
    stopped = threading.Event()

    def beat() -> None:
        while not stopped.wait(interval_seconds):
            emit_progress("target_heartbeat", path=path,
                          elapsed_seconds=round(time.monotonic() - started, 1))

    thread = threading.Thread(target=beat, name="mutation-target-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=max(1.0, interval_seconds))


def manifest_identity(manifest: dict) -> str:
    """Stable identity used to prove independently-run shards describe one manifest."""
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def target_budget_seconds(target: dict) -> int:
    """Return the measured one-worker cost used for sharding and campaign admission.

    ``session_deadline_seconds`` remains the hard kill limit for a single target.  Treating that
    limit as expected work made the 39-target critical lane declare 122 hours even though its last
    complete run took 8.9 hours with four workers.  Cost and timeout are different controls: the
    former must come from observed runtime and the latter bounds pathological execution.
    """
    measured = target.get("estimated_worker_seconds")
    if measured is None:
        # Synthetic/local manifests and the non-critical full campaign retain the conservative
        # timeout fallback.  The repository contract separately requires measured costs for every
        # critical target, so the scheduled lane cannot take this compatibility path.
        measured = target.get("session_deadline_seconds", 3600)
    value = int(measured or 0)
    if value <= 0:
        raise ValueError(
            f"{target.get('path')}: estimated_worker_seconds must be a positive measured cost"
        )
    return value


def select_shard(targets: list[dict], count: int, index: int) -> list[dict]:
    """Select one deterministic, disjoint round-robin partition of the target list."""
    if count <= 0:
        raise ValueError("--shard-count must be positive")
    if index < 0 or index >= count:
        raise ValueError(f"--shard-index must be between 0 and {count - 1}")
    # Greedy longest-processing-time assignment gives every shard a comparable declared cost.
    # Round-robin by manifest position looked balanced by target count while placing several
    # six-hour targets together.  Preserve manifest order inside each fragment so reports remain
    # stable and readable.
    positions = {id(target): position for position, target in enumerate(targets)}
    shards: list[list[dict]] = [[] for _ in range(count)]
    totals = [0] * count
    ordered = sorted(targets, key=lambda target: (-target_budget_seconds(target),
                                                  target["path"]))
    for target in ordered:
        destination = min(range(count), key=lambda shard: (totals[shard], shard))
        shards[destination].append(target)
        totals[destination] += target_budget_seconds(target)
    return sorted(shards[index], key=lambda target: positions[id(target)])


def validate_campaign_budget(targets: list[dict], deadline_seconds: float | None,
                             workers: int = 1, margin: float = 0.20) -> dict:
    """Reject a campaign whose declared target work cannot fit its wall-clock SLO."""
    if deadline_seconds is None:
        raise ValueError("mutation campaign requires --deadline-seconds")
    if not 0 <= margin < 1:
        raise ValueError("mutation campaign budget margin must be in [0, 1)")
    if workers <= 0:
        raise ValueError("mutation campaign workers must be positive")
    declared_worker_seconds = sum(target_budget_seconds(target) for target in targets)
    declared = math.ceil(declared_worker_seconds / workers)
    usable = deadline_seconds * (1 - margin)
    if declared > usable:
        raise ValueError(
            f"mutation shard declares {declared}s of measured wall-clock work at {workers} "
            f"worker(s), but only {usable:.0f}s "
            f"fits inside the {deadline_seconds:.0f}s campaign deadline with {margin:.0%} margin"
        )
    return {"declared_worker_seconds": declared_worker_seconds,
            "declared_wall_seconds": declared, "workers": workers,
            "deadline_seconds": deadline_seconds, "margin": margin,
            "usable_seconds": usable}


def coverage_exempt_lines(path: Path) -> set[int]:
    """Line numbers the coverage policy has deliberately excluded with a no-cover pragma.

    A mutant on such a line cannot be killed, and asking for one is incoherent: the pragma IS the
    statement that nothing exercises this line. Every one of them in this repo is an optional-import
    or C-extension fallback -- `libyaml` absent from a minimal PyYAML build, a runtime dep missing
    in a host test env -- reachable only in an environment the suite does not construct.

    Without this, editing a pragma line becomes an un-passable gate: the diff-scoped campaign
    mutates the very `except ImportError:` the pragma exempts, the mutant survives because nothing
    tests it, and the score goes to zero for a comment change (okengine#466 surfaced exactly this,
    six targets at once).

    Same shape as the postponed-annotation rule above: a mutant that is unkillable for a
    STRUCTURAL reason is classified, not counted against the tests.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {number for number, line in enumerate(source.splitlines(), 1)
            if re.search(r"#\s*pragma:\s*no cover", line)}


def postponed_union_positions(path: Path) -> set[tuple[int, int]]:
    """Return ``|`` token positions that belong only to postponed annotations.

    Cosmic Ray mutates the union token even when ``from __future__ import annotations`` makes the
    annotation inert at runtime.  Classifying that syntactic class here is more reliable and more
    auditable than line-number dispositions, which silently drift whenever production code moves.
    Runtime bitwise-or expressions are deliberately excluded.
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise ValueError(f"cannot classify postponed annotations in {path}: {exc}") from exc
    postponed = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )
    if not postponed:
        return set()
    spans: list[tuple[int, int, int, int]] = []

    def remember(annotation: ast.AST | None) -> None:
        if annotation is not None:
            spans.append((annotation.lineno, annotation.col_offset,
                          annotation.end_lineno, annotation.end_col_offset))

    for node in ast.walk(tree):
        if isinstance(node, ast.arg):
            remember(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            remember(node.returns)
        elif isinstance(node, ast.AnnAssign):
            remember(node.annotation)

    def inside(line: int, column: int, span: tuple[int, int, int, int]) -> bool:
        start_line, start_col, end_line, end_col = span
        return (line, column) >= (start_line, start_col) and (line, column) < (end_line, end_col)

    return {
        (token.start[0], token.start[1])
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.OP and token.string == "|"
        and any(inside(token.start[0], token.start[1], span) for span in spans)
    }


def fingerprint(mutation: dict) -> str:
    position = mutation.get("start_pos", mutation.get("start-position", []))
    line, column = position if len(position) == 2 else ("?", "?")
    module = mutation.get("module_path", mutation.get("module-path", "?"))
    operator = mutation.get("operator_name", mutation.get("operator-name", "?"))
    occurrence = mutation.get("occurrence", "?")
    return f"{module}:{line}:{column}:{operator}:{occurrence}"


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot load {path}: {exc}") from exc


def uncommitted_paths(repo: Path) -> list[str] | None:
    """Working-tree paths a campaign cannot see — or None when git could not be asked.

    Every campaign runs in a detached worktree at HEAD (see `worker_pool`), which is right: mutating
    a dirty tree would not be reproducible. The consequence is that an UNCOMMITTED test does not
    exist as far as the gate is concerned, and the failure mode is not an error — it is a *plausible
    surviving mutant*, which reads exactly like "your tests are too weak".

    Cost an hour on okengine#600: a boundary test that killed a mutant by hand, verified against the
    target's exact test command, kept showing as a survivor across a clean session and at --jobs 1.
    It was not committed. `git commit` turned 88.89% into 100%.

    None rather than [] when git fails, because "the tree is clean" and "I could not find out" are
    different answers and only one of them means the results describe what you are looking at.
    """
    try:
        proc = subprocess.run(["git", "status", "--porcelain"], cwd=repo, text=True,
                              capture_output=True)
    except OSError:
        # An unreadable/absent directory, or no git on PATH. Both are "could not ask", and neither
        # is a reason to take down a campaign that is otherwise fine.
        return None
    if proc.returncode:
        return None
    return sorted(line[3:].strip() for line in proc.stdout.splitlines() if line.strip())


def worktree_visibility_warning(dirty: list[str] | None) -> str | None:
    """The operator-facing sentence for what the campaign could not see, if anything."""
    if dirty is None:
        return ("cannot tell whether the working tree is clean (git failed); these results describe "
                "HEAD, which may not be what you are editing")
    if not dirty:
        return None
    tests = [path for path in dirty if path.startswith("tests/")]
    detail = ", ".join(dirty[:8]) + ("..." if len(dirty) > 8 else "")
    lead = (f"{len(dirty)} uncommitted path(s) are INVISIBLE to this campaign — it measures HEAD in "
            f"a detached worktree: {detail}")
    if tests:
        lead += (f" · {len(tests)} of them are tests, so a survivor here may already be dead at your "
                 f"working tree; commit before reading the score")
    return lead



def newly_critical_targets(repo: Path, base: str, manifest: dict) -> list[str]:
    """Targets this diff promotes to `critical: true` — new, or flipped from not-critical.

    Promotion is the failure this exists to catch. Classifying a module critical asserts its
    survivors are triaged, but nothing verified that at authoring time: the critical campaign is
    scheduled, so the first proof arrived on the next nightly.
    okengine-mcp/output_contract_enforce.py was promoted on 2026-09-10 with 14 untriaged
    survivors and main failed nightly from 09-11 until someone looked.

    Returns [] when the manifest is untouched, which is almost every merge request — the check
    then costs one `git show` and nothing else.
    """
    proc = subprocess.run(["git", "show", f"{base}:mutation/targets.json"],
                          cwd=repo, text=True, capture_output=True)
    now_critical = {t["path"] for t in manifest["targets"] if t.get("critical")}
    if proc.returncode:
        # No baseline to compare against (shallow clone, manifest just added). Treat every
        # critical target as newly promoted rather than silently checking nothing: an unknown
        # baseline must not read as "nothing changed".
        return sorted(now_critical)
    try:
        before = json.loads(proc.stdout)
    except ValueError:
        return sorted(now_critical)
    was_critical = {t["path"] for t in before.get("targets", []) if t.get("critical")}
    return sorted(now_critical - was_critical)


def changed_paths(repo: Path, base: str) -> set[str]:
    # Deleted modules have no post-image to mutate. Including them makes removal impossible unless
    # a manifest target points at a file that no longer exists, which the manifest validator then
    # correctly rejects. Rename/copy destinations and all other non-deleted changes remain in scope.
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRTUXB", f"{base}...HEAD"], cwd=repo,
        text=True, capture_output=True,
    )
    if proc.returncode:
        raise RuntimeError(f"cannot resolve mutation diff against {base}: {proc.stderr.strip()}")
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(repo: Path, base: str, path: str) -> set[int]:
    """Post-image line numbers of `path` touched by the diff against `base`.

    This is what makes a first touch of a legacy module affordable. Without it a changed module is
    scored across its WHOLE body, so a small fix inherits every mutant the file ever accumulated —
    measured on okengine#549, 18 of 20 survivors were in code the MR never touched, and
    okengine-mcp/write_server.py (~2,172 statements) was effectively uneditable on those terms.
    """
    proc = subprocess.run(
        ["git", "diff", "--unified=0", f"{base}...HEAD", "--", path], cwd=repo,
        text=True, capture_output=True,
    )
    if proc.returncode:
        raise RuntimeError(f"cannot resolve changed lines for {path}: {proc.stderr.strip()}")
    lines: set[int] = set()
    for row in proc.stdout.splitlines():
        m = _HUNK.match(row)
        if m:
            start, count = int(m.group(1)), int(m.group(2) or 1)
            lines.update(range(start, start + count))
    return lines


def skip_out_of_scope(session_path: Path, in_scope: set[int]) -> tuple[int, int]:
    """Mark every mutant outside `in_scope` as SKIPPED before `exec` runs. Returns (skipped, kept).

    Diff-scoping the FLOOR (okengine#552) made a small change to a large module affordable to
    satisfy, but not affordable to RUN: Cosmic Ray still generated and executed every mutant in the
    file, so a 64-line change to okengine-mcp/write_server.py (~2,172 statements) still cost a
    multi-hour campaign. Scoring-time filtering cannot fix that — the work is already done by then.

    Marking the work items complete up front is how Cosmic Ray's own filters (cr-filter-pragma,
    cr-filter-operators) narrow a session, so this uses the supported mechanism rather than editing
    the session database behind its back. `exec` then has nothing to do for those mutants, and
    score_target's `in_scope` check drops them before they can be read as infrastructure errors.
    """
    # `use_db` is the module-level opener; WorkDB itself has no `.open` classmethod. Importing the
    # real symbol (rather than reaching for a plausible-looking one) is the point — see the contract
    # test in tests/ci/test_mutation_gate.py, which exercises this against a real session db.
    from cosmic_ray.work_db import WorkDB, use_db
    from cosmic_ray.work_item import WorkerOutcome, WorkResult

    skipped = kept = 0
    with use_db(session_path, WorkDB.Mode.open) as db:
        for item in list(db.pending_work_items):
            mutations = getattr(item, "mutations", None) or []
            pos = getattr(mutations[0], "start_pos", None) if mutations else None
            if pos and len(pos) == 2 and pos[0] in in_scope:
                kept += 1
                continue
            skipped += 1
            db.set_result(item.job_id, WorkResult(
                worker_outcome=WorkerOutcome.SKIPPED,
                output="okengine: outside the MR diff (okengine#552 diff-scoped run)"))
    return skipped, kept



def dump_session(session_path: Path, out_path: Path) -> None:
    """Write a Cosmic Ray session to JSONL, tolerating results that carry no test outcome.

    Upstream's `cosmic-ray dump` does `d["test_outcome"].value` unconditionally, so it raises
    AttributeError on any result whose test_outcome is None. Its OWN skip filter writes exactly
    that -- pragma_no_mutate emits `WorkResult(worker_outcome=SKIPPED, test_outcome=None)` -- so
    `dump` cannot read back a session its own filters produced. skip_out_of_scope writes results in
    the same (correct) shape, so rather than fake a test outcome that never happened, emit the
    identical wire format in-process and skip the crashing reader entirely (okengine#552).

    The format is upstream's, byte for byte: one JSON list per line, `[work_item, result_or_null]`,
    completed items first, then pending.
    """
    from attr import asdict
    from cosmic_ray.work_db import WorkDB, use_db

    def item_to_dict(work_item):
        d = asdict(work_item)
        for mutation in d["mutations"]:
            mutation["module_path"] = str(mutation["module_path"])
        return d

    def result_to_dict(result):
        d = asdict(result)
        # Unwrap the enum only when one is actually present -- this `is not None` IS the fix.
        for key in ("worker_outcome", "test_outcome"):
            d[key] = d[key].value if d[key] is not None else None
        return d

    lines = []
    with use_db(session_path, WorkDB.Mode.open) as db:
        for work_item, result in db.completed_work_items:
            lines.append(json.dumps((item_to_dict(work_item), result_to_dict(result))))
        for work_item in db.pending_work_items:
            lines.append(json.dumps((item_to_dict(work_item), None)))
    # No trailing newline for an empty session: parse_dump must still fail loudly with
    # "zero mutants" rather than choke on a blank line.
    out_path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")

def select_targets(manifest: dict, mode: str, changed: set[str]) -> tuple[list[dict], list[str]]:
    targets = manifest.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("mutation manifest must contain a non-empty targets list")
    paths = [target.get("path") for target in targets]
    if any(not isinstance(path, str) or not path.endswith(".py") for path in paths):
        raise ValueError("every mutation target requires a Python path")
    if len(paths) != len(set(paths)):
        raise ValueError("mutation target paths must be unique")
    for target in targets:
        minimum = target.get("min_mutants")
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
            raise ValueError(
                f"{target.get('path')}: min_mutants must be a positive integer"
            )
    if mode == "full":
        return targets, []
    production = {
        path for path in changed
        if path.endswith(".py") and path.startswith(PRODUCTION_ROOTS)
    }
    missing = sorted(production - set(paths))
    return [target for target in targets if target["path"] in changed], missing


def cosmic_config(target: dict, worker_urls: list[str] | None = None) -> str:
    command = target.get("test_command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError(f"{target.get('path')}: non-empty test_command is required")
    timeout = int(target.get("timeout_seconds", 60))
    if timeout <= 0:
        raise ValueError(f"{target.get('path')}: timeout_seconds must be positive")
    command = command.replace("{python}", sys.executable)
    # Mutation testing asks a YES/NO question -- did ANY test fail? -- so there is nothing to gain
    # from running the rest of the suite once one has. `-x` stops at the first failure, which is
    # pure saving on every KILLED mutant and changes nothing for a survivor (which runs to the end
    # either way).
    #
    # MEASURED 2026-08-23 on okengine-mcp/write_server.py, identical 24-mutant sample, same worker
    # count, only this flag varying:
    #
    #     baseline   390s   15 KILLED / 9 SURVIVED
    #     with -x    267s   15 KILLED / 9 SURVIVED     -31.5%, outcomes unchanged
    #
    # The unchanged outcome distribution is the load-bearing half: a "speedup" that altered which
    # mutants were killed would be silently corrupting the score rather than saving time.
    #
    # Applied HERE and not in the manifest's 80 test_command strings, so it is enforced where every
    # target crosses and cannot drift per entry. A target that genuinely needs the whole suite can
    # opt out with `"exit_first": false`.
    parts = shlex.split(command)
    if (target.get("exit_first", True) and "pytest" in parts
            and not {"-x", "--exitfirst"} & set(parts)):
        parts.append("-x")
        command = shlex.join(parts)
    # Cosmic Ray's local worker timeout can terminate its immediate runner while
    # leaving pytest descendants alive with the result pipe open.  In CI that
    # produced a four-hour "45 second" mutant and also defeated the surrounding
    # session deadline.  Put an independently tested process-group watchdog at
    # the actual test-command boundary; retain Cosmic Ray's slightly wider
    # timeout as a second fail-closed layer.
    guarded_command = shlex.join([
        sys.executable, "ci/mutation_timeout.py", "--seconds", str(timeout), "--",
        *shlex.split(command),
    ])
    escaped_path = target["path"].replace('"', '\\"')
    escaped_command = guarded_command.replace('\\', '\\\\').replace('"', '\\"')
    distributor = (
        "[cosmic-ray.distributor]\nname = \"http\"\n\n"
        "[cosmic-ray.distributor.http]\n"
        f"worker-urls = {json.dumps(worker_urls)}\n"
        if worker_urls else "[cosmic-ray.distributor]\nname = \"local\"\n"
    )
    return (
        "[cosmic-ray]\n"
        f'module-path = "{escaped_path}"\n'
        f"timeout = {timeout + 10}.0\n"
        "excluded-modules = []\n"
        f'test-command = "{escaped_command}"\n\n'
        f"{distributor}"
    )


def run_command(command: list[str], repo: Path, deadline: int, output: Path | None = None,
                env: dict[str, str] | None = None) -> str:
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            command, cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, env=env,
        )
        stdout, stderr = proc.communicate(timeout=deadline)
    except subprocess.TimeoutExpired as exc:
        assert proc is not None
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.communicate(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
        raise RuntimeError(f"command timed out after {deadline}s: {' '.join(command)}") from exc
    except OSError as exc:
        raise RuntimeError(f"cannot execute {' '.join(command)}: {exc}") from exc
    if proc.returncode:
        detail = (stderr or stdout).strip()[-2000:]
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(command)}\n{detail}")
    if output is not None:
        output.write_text(stdout, encoding="utf-8")
    return stdout


@contextmanager
def isolated_checkout(repo: Path):
    """Run mutators away from the developer/runner checkout they rewrite in place."""
    with tempfile.TemporaryDirectory(prefix="okengine-mutation-") as parent:
        checkout = Path(parent) / "checkout"
        run_command(["git", "worktree", "add", "--detach", str(checkout), "HEAD"], repo, 120)
        try:
            yield checkout
        finally:
            run_command(["git", "worktree", "remove", "--force", str(checkout)], repo, 120)


def mutation_environment(checkout: Path) -> dict[str, str]:
    """Import packaged code from the checkout Cosmic Ray is mutating.

    CI installs the engine wheel before the mutation job and developer environments commonly use
    an editable install.  Without an explicit source-root precedence, pytest imports that installed
    copy while Cosmic Ray rewrites ``checkout/src``.  The campaign then reports survivors (or a
    perfect score) for code the tests never loaded.  Keep ordinary environment values, but make the
    isolated checkout authoritative for the package under test.
    """
    env = os.environ.copy()
    # The engine has three importable production roots: the packaged ``src`` tree plus legacy
    # top-level modules loaded by the MCP server and cron scripts. Prioritizing only ``src`` left
    # imports such as ``id_lib`` free to resolve from an editable install outside this detached
    # SHA, producing either a broken baseline or a convincing score for the wrong code.
    roots = [
        checkout / "src",
        checkout / "scripts" / "cron",
        checkout / "okengine-mcp",
        checkout,
    ]
    existing = env.get("PYTHONPATH")
    values = [str(path) for path in roots]
    if existing:
        values.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(values)
    # Every HTTP worker launches hundreds of independent pytest processes. Pytest's tmp_path
    # retention cleanup is process-local, so workers sharing the host's /tmp can race: one process
    # removes another's live vault and the test then observes missing pages or stale indexes. A
    # fresh Git worktree does not isolate that external state. Keep all temporary files inside the
    # worker/baseline checkout so concurrent campaigns cannot reuse or reap each other's paths.
    # Keep the component non-hidden. Several production scanners intentionally reject a page when
    # any component below the vault is dot-prefixed. Pytest places tmp_path below TMPDIR, so a
    # hidden temp root makes valid fixture pages look hidden and breaks otherwise-clean baselines.
    temp_root = checkout / "mutation-tmp"
    temp_root.mkdir(exist_ok=True)
    env["TMPDIR"] = str(temp_root)
    return env


def descendant_process_groups(root_pid: int) -> set[int]:
    """Return Linux process groups below ``root_pid`` before their parent exits.

    ``mutation_timeout.py`` deliberately starts pytest in a new session so its own timeout can
    terminate the entire test tree.  That also means killing only a Cosmic Ray HTTP worker's
    process group can strand an in-flight watchdog's pytest group.  Snapshot the descendant tree
    while the worker is still alive so cleanup can signal every independently created group.

    The mutation jobs run on Linux.  On another platform (or a restricted ``/proc`` mount), fail
    closed to the worker's own group rather than making the gate unavailable.
    """
    pending = [root_pid]
    seen: set[int] = set()
    groups: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            children_text = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="utf-8")
        except OSError:
            continue
        for value in children_text.split():
            try:
                child = int(value)
                group = os.getpgid(child)
            except (ValueError, ProcessLookupError, PermissionError):
                continue
            pending.append(child)
            groups.add(group)
    return groups


def signal_process_groups(groups: set[int], sig: signal.Signals) -> None:
    """Signal process groups without ever targeting the mutation gate's own group."""
    own_group = os.getpgrp()
    for group in sorted(groups):
        if group <= 0 or group == own_group:
            continue
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            pass


@contextmanager
def verified_baseline_checkout(repo: Path, cosmic: list[str], config_path: Path,
                               target: dict, deadline: int):
    """Yield a fresh checkout whose mutation baseline has passed.

    A baseline failure cannot produce a meaningful mutation score.  One transient failure used to
    discard every mutant for a target even when the identical suite passed immediately before and
    after the campaign.  Retry once in a *new* worktree: this recovers from leaked checkout state
    while a deterministic test or source failure still fails closed on both attempts.  Preserve
    both diagnostics so the retry does not turn an intermittent failure into invisible success.
    """
    command = cosmic + ["baseline", str(config_path)]
    first_failure: str
    with isolated_checkout(repo) as checkout:
        env = mutation_environment(checkout)
        try:
            run_command(command, checkout, deadline, env=env)
        except RuntimeError as exc:
            first_failure = str(exc)
            print(
                f"{target['path']}: mutation baseline attempt 1/2 failed in {checkout}",
                file=sys.stderr,
            )
        else:
            yield checkout, env
            return

    with isolated_checkout(repo) as checkout:
        env = mutation_environment(checkout)
        try:
            run_command(command, checkout, deadline, env=env)
        except RuntimeError as exc:
            print(
                f"{target['path']}: mutation baseline attempt 2/2 failed in {checkout}",
                file=sys.stderr,
            )
            raise RuntimeError(
                "mutation baseline failed in two fresh checkouts\n"
                f"first attempt: {first_failure}\nsecond attempt: {exc}"
            ) from exc
        print(
            f"{target['path']}: mutation baseline recovered on fresh-checkout attempt 2/2",
            file=sys.stderr,
        )
        yield checkout, env


def baseline_signature(target: dict) -> tuple[str, int, bool]:
    """Identify targets whose Cosmic Ray baseline executes the same effective suite."""
    command = target.get("test_command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError(f"{target.get('path')}: non-empty test_command is required")
    timeout = int(target.get("timeout_seconds", 60))
    if timeout <= 0:
        raise ValueError(f"{target.get('path')}: timeout_seconds must be positive")
    return command, timeout, bool(target.get("exit_first", True))


def preflight_baselines(repo: Path, cosmic: list[str], artifacts: Path,
                        targets: list[dict]) -> dict:
    """Validate each distinct baseline once before starting any mutation work.

    Baseline does not mutate the configured module; it only executes the effective test command.
    Repeating one broken write-service suite for thirteen module entries delayed the same known
    failure and left a partial campaign.  Grouping by command, timeout, and exit-first semantics
    makes the shared prerequisite explicit and fails before the first mutant is created.
    """
    groups: dict[tuple[str, int, bool], list[dict]] = {}
    for target in targets:
        groups.setdefault(baseline_signature(target), []).append(target)
    preflight_dir = artifacts / "baseline-preflight"
    preflight_dir.mkdir(parents=True, exist_ok=True)
    checked: list[dict] = []
    for position, grouped in enumerate(groups.values(), 1):
        representative = grouped[0]
        config_path = preflight_dir / f"group-{position}.toml"
        config_path.write_text(cosmic_config(representative), encoding="utf-8")
        paths = [target["path"] for target in grouped]
        emit_progress("baseline_preflight_start", group_index=position,
                      group_count=len(groups), representative_path=representative["path"],
                      target_count=len(paths))
        try:
            with verified_baseline_checkout(
                repo, cosmic, config_path, representative,
                target_budget_seconds(representative),
            ):
                pass
        except RuntimeError as exc:
            emit_progress("baseline_preflight_complete", group_index=position,
                          outcome="error", representative_path=representative["path"],
                          target_count=len(paths))
            raise RuntimeError(
                f"baseline preflight failed for {len(paths)} target(s) sharing "
                f"{representative['path']}: {', '.join(paths)}\n{exc}"
            ) from exc
        checked.append({"representative_path": representative["path"], "targets": paths})
        emit_progress("baseline_preflight_complete", group_index=position, outcome="passed",
                      representative_path=representative["path"], target_count=len(paths))
    return {"group_count": len(groups), "groups": checked}


def validate_baseline_proof(path: Path, manifest_sha256: str,
                            targets: list[dict]) -> dict:
    """Require a successful, exact-manifest preflight covering every selected target."""
    proof = load_json(path)
    if proof.get("manifest_sha256") != manifest_sha256:
        raise ValueError("baseline preflight proof does not match this manifest")
    covered = set(proof.get("selected_paths") or [])
    missing = sorted(target["path"] for target in targets if target["path"] not in covered)
    if missing:
        raise ValueError("baseline preflight proof does not cover: " + ", ".join(missing))
    if proof.get("status") != "passed":
        raise ValueError("baseline preflight proof is not successful")
    return proof


@contextmanager
def cosmic_http_workers(repo: Path, cosmic: list[str], count: int):
    """Run isolated Cosmic Ray HTTP workers for parallel mutants within one module."""
    processes: list[subprocess.Popen] = []
    process_groups: dict[int, set[int]] = {}
    urls: list[str] = []
    with ExitStack() as stack:
        try:
            for _ in range(count):
                checkout = stack.enter_context(isolated_checkout(repo))
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = reservation.getsockname()[1]
                process = subprocess.Popen(
                    cosmic + ["http-worker", "--port", str(port)], cwd=checkout,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, env=mutation_environment(checkout),
                )
                processes.append(process)
                urls.append(f"http://127.0.0.1:{port}")
            ready_by = time.monotonic() + 20
            pending = set(range(len(processes)))
            while pending and time.monotonic() < ready_by:
                for index in list(pending):
                    process = processes[index]
                    if process.poll() is not None:
                        raise RuntimeError(
                            f"Cosmic Ray HTTP worker exited during startup ({process.returncode})")
                    try:
                        urllib.request.urlopen(urls[index], timeout=0.2)
                    except urllib.error.HTTPError:
                        pending.remove(index)  # 404/405 proves the HTTP listener is ready.
                    except (OSError, urllib.error.URLError):
                        continue
                    else:
                        pending.remove(index)
                if pending:
                    time.sleep(0.05)
            if pending:
                raise RuntimeError(f"Cosmic Ray HTTP workers did not start within 20s: {len(pending)}")
            yield urls
        finally:
            for process in processes:
                if process.poll() is None:
                    groups = descendant_process_groups(process.pid) | {process.pid}
                    signal_process_groups(groups, signal.SIGTERM)
                    # Retain the snapshot: once the worker exits, an escaped new-session pytest
                    # process is reparented and can no longer be discovered below its old parent.
                    process_groups[process.pid] = groups
            for process in processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    groups = process_groups.get(process.pid, {process.pid})
                    signal_process_groups(groups, signal.SIGKILL)
                    process.wait()


def parse_dump(path: Path, *, allow_empty: bool = False) -> list[tuple[dict, dict]]:
    rows = []
    try:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            value = json.loads(line)
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError(f"line {number} is not a work-item/result pair")
            rows.append((value[0], value[1]))
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid Cosmic Ray dump {path}: {exc}") from exc
    if not rows and not allow_empty:
        raise ValueError(f"zero mutants in {path}")
    return rows


def score_target(target: dict, rows: list[tuple[dict, dict]], dispositions: dict,
                 annotation_unions: set[tuple[int, int]] | None = None,
                 exempt_lines: set[int] | None = None,
                 in_scope: set[int] | None = None) -> dict:
    """Score one target. `in_scope`, when given, restricts scoring to mutants on those lines.

    Out-of-scope mutants are counted and reported but neither scored nor required to carry a
    disposition: they are pre-existing debt, and billing it to whoever next touches the file taxes
    the wrong person and discourages exactly the small fixes worth encouraging (okengine#552).
    """
    result = {
        "path": target["path"], "critical": bool(target.get("critical")),
        "category": target.get("category", "general"), "owner": target.get("owner"),
        "total": len(rows), "killed": 0, "survived": 0, "equivalent": 0,
        "out_of_scope": 0, "scoped_lines": (len(in_scope) if in_scope is not None else None),
        "infrastructure_errors": [], "survivors": [],
        "min_mutants": target.get("min_mutants"),
        "floor_waiver": target.get("floor_waiver"),
    }
    for item, work_result in rows:
        if not isinstance(item, dict) or not isinstance(work_result, dict):
            result["infrastructure_errors"].append("missing or malformed work result")
            continue
        mutations = item.get("mutations") or []
        if len(mutations) != 1:
            result["infrastructure_errors"].append("work item did not contain exactly one mutation")
            continue
        mutation = mutations[0]
        key = fingerprint(mutation)
        if in_scope is not None:
            pos = mutation.get("start_pos", mutation.get("start-position", []))
            if not (len(pos) == 2 and pos[0] in in_scope):
                result["out_of_scope"] += 1
                continue
        worker = work_result.get("worker_outcome")
        outcome = work_result.get("test_outcome")
        output_text = str(work_result.get("output") or "")
        timed_out = output_text == "timeout" or TIMEOUT_SENTINEL in output_text
        if worker != "normal" or outcome in INFRA_OUTCOMES or timed_out:
            result["infrastructure_errors"].append(
                f"{key}: worker={worker!r}, test={outcome!r}, timeout={timed_out}"
            )
        elif outcome == "killed":
            result["killed"] += 1
        elif outcome == "survived":
            disposition = dispositions.get(key)
            position = mutation.get("start_pos", mutation.get("start-position", []))
            if (
                disposition is None
                and mutation.get("operator_name", mutation.get("operator-name", "")).startswith(
                    "core/ReplaceBinaryOperator_BitOr_"
                )
                and len(position) == 2
                and tuple(position) in (annotation_unions or set())
            ):
                disposition = {
                    "disposition": "equivalent",
                    "owner": target.get("owner"),
                    "approved_by": "mutation-gate:postponed-annotation",
                    "rationale": "The mutated token is inside an annotation postponed by "
                                 "from __future__ import annotations and cannot affect runtime behavior.",
                }
            if (
                disposition is None
                and len(position) >= 1
                and position[0] in (exempt_lines or set())
            ):
                disposition = {
                    "disposition": "equivalent",
                    "owner": target.get("owner"),
                    "approved_by": "mutation-gate:coverage-exempt",
                    "rationale": "The mutated line carries a coverage-exclusion pragma — the "
                                 "coverage "
                                 "policy states that nothing exercises it, so no test can kill a "
                                 "mutant there. See tests/test_coverage_exclusion_policy.py, which "
                                 "bounds how many such lines may exist and requires each to say why.",
                }
            if disposition is None:
                for rule in dispositions.get("__patterns__", []):
                    if fnmatch.fnmatchcase(key, rule["pattern"]):
                        disposition = {name: value for name, value in rule.items() if name != "pattern"}
                        break
            survivor = {"fingerprint": key, "mutation": mutation, "disposition": disposition}
            result["survivors"].append(survivor)
            if (
                isinstance(disposition, dict)
                and disposition.get("disposition") == "equivalent"
                and disposition.get("owner")
                and disposition.get("rationale")
                and disposition.get("approved_by")
            ):
                result["equivalent"] += 1
            else:
                result["survived"] += 1
        else:
            result["infrastructure_errors"].append(f"{key}: unknown test outcome {outcome!r}")
    denominator = result["killed"] + result["survived"]
    result["score"] = round(100 * result["killed"] / denominator, 2) if denominator else None
    return result


def floor_waiver_status(result: dict, today: dt.date | None = None) -> tuple[bool, str | None]:
    """Validate a temporary, explicit waiver for one below-floor target.

    Waivers never alter the measured score or survivor accounting. They only
    suppress the floor error until an ISO expiry date, with named ownership,
    approval, rationale, and tracking issue retained in the report.
    """
    waiver = result.get("floor_waiver")
    if waiver is None:
        return False, None
    path = result.get("path", "<unknown>")
    if not isinstance(waiver, dict):
        return False, f"{path}: floor_waiver must be an object"
    required = ("owner", "approved_by", "rationale", "issue", "expires")
    missing = [key for key in required if not str(waiver.get(key) or "").strip()]
    if missing:
        return False, f"{path}: floor_waiver missing required field(s): {', '.join(missing)}"
    try:
        expiry = dt.date.fromisoformat(str(waiver["expires"]))
    except ValueError:
        return False, f"{path}: floor_waiver expires must be an ISO date"
    current = today or dt.datetime.now(dt.timezone.utc).date()
    if expiry < current:
        return False, f"{path}: floor_waiver expired on {expiry.isoformat()}"
    return True, None


def unrunnable_target(target: dict, reason: str) -> dict:
    """A result for a target whose campaign could not run at all.

    The alternative -- letting the exception escape -- discarded every target already COMPLETED,
    because the driver built `report["targets"]` with a list comprehension. Measured: the scheduled
    full campaign on main died on target 14 of 64 after 3h17m and reported `killed: 0, survived: 0,
    score: null`, which is indistinguishable from "there was nothing to measure". Thirteen finished
    targets were thrown away, and their artifacts were sitting in the upload.

    The failure is recorded as an infrastructure error on THIS target, so it still fails the gate
    (compliance_errors turns those into hard errors) while everything else keeps its score.
    """
    return {
        "path": target["path"], "critical": bool(target.get("critical")),
        "category": target.get("category", "general"), "owner": target.get("owner"),
        "total": 0, "killed": 0, "survived": 0, "equivalent": 0,
        "out_of_scope": 0, "scoped_lines": None,
        "infrastructure_errors": [f"campaign did not run: {reason}"], "survivors": [],
        "score": None, "min_mutants": target.get("min_mutants"),
        "floor_waiver": target.get("floor_waiver"),
    }


def aggregate(results: list[dict], critical_only: bool = False) -> dict:
    selected = [result for result in results if result["critical"] or not critical_only]
    killed = sum(result["killed"] for result in selected)
    survived = sum(result["survived"] for result in selected)
    denominator = killed + survived
    # A target whose campaign could not run contributes 0 killed and 0 survived, so it leaves the
    # denominator SILENTLY: the score below is then computed over a smaller surface than the
    # manifest describes, and gets printed beside "campaign did not run" as though the two were
    # commensurable. Measured on the 2026-08-22 nightly: `critical mutation score 85.85%` was the
    # score of 13 targets, with okengine-mcp/write_server.py -- the enforced write path, and the
    # single most critical file in the manifest -- contributing nothing at all (okengine#612).
    # Carry the gap in the aggregate so no consumer has to cross-reference two lists to notice it.
    unmeasured = sorted(result["path"] for result in selected
                        if result["score"] is None and result.get("infrastructure_errors"))
    return {
        "killed": killed, "survived": survived,
        "equivalent": sum(result["equivalent"] for result in selected),
        "score": round(100 * killed / denominator, 2) if denominator else None,
        "unmeasured": unmeasured,
        "complete": not unmeasured,
    }


def partial_marker(aggregated: dict) -> str:
    """Suffix naming what an aggregate score was computed WITHOUT, or '' when it is complete."""
    missing = aggregated.get("unmeasured") or []
    if not missing:
        return ""
    shown = ", ".join(missing[:4]) + (" ..." if len(missing) > 4 else "")
    return f" [PARTIAL — computed WITHOUT {len(missing)} unmeasured target(s): {shown}]"


def compliance_errors(report: dict, overall_floor: float, critical_floor: float) -> list[str]:
    errors = list(report.get("errors", []))
    waiver_active: dict[str, bool] = {}
    for result in report["targets"]:
        active, waiver_error = floor_waiver_status(result)
        waiver_active[result["path"]] = active
        if waiver_error:
            errors.append(waiver_error)
    # A target the campaign never reached is UNKNOWN, not passing. Saying so is the whole point of
    # publishing partial results: a run that measured 40 of 73 targets has a real score for 40 and
    # no opinion about the other 33, and must not read as a clean sweep of the manifest.
    not_measured = report.get("not_measured") or []
    if not_measured:
        errors.append(
            f"{len(not_measured)} target(s) not measured — the campaign ran out of budget before "
            f"reaching them, so their score is unknown: " + ", ".join(sorted(not_measured)[:8])
            + (" ..." if len(not_measured) > 8 else "")
        )
    for result in report["targets"]:
        errors.extend(f"{result['path']}: {error}" for error in result["infrastructure_errors"])
        minimum = result.get("min_mutants")
        if report.get("mode", "full") == "full" and minimum is not None \
                and result["total"] < minimum:
            errors.append(
                f"{result['path']}: generated {result['total']} mutant(s), below declared "
                f"min_mutants {minimum}"
            )
        for survivor in result["survivors"]:
            disposition = survivor["disposition"]
            # A target-level floor waiver is also the explicit, dated ownership record for its
            # assertion gaps. Keep every survivor in the denominator and report, but do not demand
            # thousands of duplicate per-fingerprint records carrying the same owner and expiry.
            # Invalid/expired waivers are inactive (and already produce their own hard error), so
            # they cannot suppress this check.
            if (
                not waiver_active[result["path"]]
                and (
                    not isinstance(disposition, dict)
                    or not disposition.get("owner")
                    or not disposition.get("disposition")
                )
            ):
                errors.append(f"{survivor['fingerprint']}: survivor lacks owner/disposition")
        target_floor = critical_floor if result["critical"] else overall_floor
        if result["score"] is None:
            if result.get("scoped_lines") is not None:
                continue          # diff-scoped and nothing mutable changed — not a failure
            if result["infrastructure_errors"]:
                continue          # the campaign could not run; that is already reported above, and
                                  # "denominator is zero" would describe a crash as an empty run
            errors.append(f"{result['path']}: mutation denominator is zero")
        elif result["score"] < target_floor and not waiver_active[result["path"]]:
            errors.append(
                f"{result['path']}: mutation score {result['score']:.2f}% "
                f"is below {target_floor:.2f}%"
            )
    overall = report["overall"]["score"]
    critical = report["critical"]["score"]
    scoped = any(r.get("scoped_lines") is not None for r in report["targets"])
    if report["targets"] and overall is None and not scoped:
        errors.append("overall mutation denominator is zero")
    elif overall is not None and overall < overall_floor and any(
        result.get("score") is not None
        and result["score"] < (critical_floor if result["critical"] else overall_floor)
        and not waiver_active[result["path"]]
        for result in report["targets"]
    ):
        errors.append(f"overall mutation score {overall:.2f}% is below {overall_floor:.2f}%"
                      + partial_marker(report["overall"]))
    elif overall is not None and not report["overall"].get("complete", True):
        # Above the floor, but over an incomplete surface. Saying nothing here would publish a
        # passing-looking number for a run that did not measure everything it claims to cover.
        errors.append(f"overall mutation score {overall:.2f}% is NOT the overall score"
                      + partial_marker(report["overall"]))
    if any(result["critical"] for result in report["targets"]):
        if critical is None and not scoped:
            errors.append("critical mutation denominator is zero")
        elif critical is not None and critical < critical_floor and any(
            result.get("critical") and result.get("score") is not None
            and result["score"] < critical_floor
            and not waiver_active[result["path"]]
            for result in report["targets"]
        ):
            errors.append(f"critical mutation score {critical:.2f}% is below {critical_floor:.2f}%"
                          + partial_marker(report["critical"]))
        elif critical is not None and not report["critical"].get("complete", True):
            errors.append(f"critical mutation score {critical:.2f}% is NOT the critical score"
                          + partial_marker(report["critical"]))
    return errors


def write_junit(path: Path, report: dict) -> None:
    suite = ET.Element("testsuite", name="mutation", tests=str(len(report["targets"]) + 1))
    failures = 0
    gate_case = ET.SubElement(suite, "testcase", classname="mutation", name="aggregate-gate")
    if report["errors"]:
        failures += 1
        ET.SubElement(gate_case, "failure", message="mutation gate failed").text = "\n".join(report["errors"])
    for result in report["targets"]:
        case = ET.SubElement(suite, "testcase", classname="mutation", name=result["path"])
        local = [error for error in report["errors"] if result["path"] in error]
        if local:
            failures += 1
            ET.SubElement(case, "failure", message="mutation gate failed").text = "\n".join(local)
    suite.set("failures", str(failures))
    path.write_bytes(ET.tostring(suite, encoding="utf-8", xml_declaration=True))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("changed", "full"), required=True)
    # Manifest-consistency only: validate the manifest and report changed production modules that
    # have no mutation target, WITHOUT building or executing a single session. This is the half of
    # the gate worth paying for on every MR -- it costs seconds and catches an unregistered module
    # the moment it appears. The campaign itself is the expensive half (okengine#558).
    parser.add_argument("--check-targets-only", action="store_true",
                        help="validate the manifest + target registration, run no campaign")
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--manifest", type=Path, default=Path("mutation/targets.json"))
    parser.add_argument("--dispositions", type=Path, default=Path("mutation/survivors.json"))
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/mutation"))
    parser.add_argument("--cosmic-ray", default=f"{sys.executable} -m cosmic_ray.cli")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--newly-critical-only", action="store_true",
                        help="select only targets this diff promotes to critical; exits clean "
                             "when the manifest declares no new ones (the common case)")
    parser.add_argument("--target", action="append", default=[],
                        help="run only this manifest path (repeatable; local focused recheck)")
    parser.add_argument("--no-diff-scope", action="store_true",
                        help="score the WHOLE file even in changed mode. The scheduled full "
                             "campaign already does this; the flag lets a reviewer reproduce a "
                             "whole-file score on demand without editing the manifest.")
    parser.add_argument("--jobs", type=int, default=1,
                        help="isolated Cosmic Ray workers for concurrent mutants within each target")
    parser.add_argument("--deadline-seconds", type=float,
                        default=float(os.environ.get("MUTATION_DEADLINE_SECONDS", "0")) or None,
                        help="stop starting new targets once this much wall clock has elapsed, "
                             "and report the ones not reached. Without it the campaign runs until "
                             "something else kills it, and a killed campaign publishes nothing.")
    parser.add_argument("--critical-only", action="store_true",
                        help="run only the manifest's critical targets — the daily signal on the "
                             "code that matters most, when the whole manifest does not fit")
    parser.add_argument("--dump", action="append", type=Path,
                        help="score existing JSONL dump(s), in selected-target order")
    parser.add_argument("--preflight-only", action="store_true",
                        help="run each unique baseline once and publish a manifest-bound proof")
    parser.add_argument("--baseline-proof", type=Path,
                        help="successful preflight proof required before sharded mutation")
    parser.add_argument("--shard-count", type=int, default=1,
                        help="split the selected campaign into deterministic fragments")
    parser.add_argument("--shard-index", type=int, default=0,
                        help="zero-based fragment to run (requires --shard-count)")
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    artifacts = (repo / args.artifacts).resolve() if not args.artifacts.is_absolute() else args.artifacts
    artifacts.mkdir(parents=True, exist_ok=True)
    report = {"mode": args.mode, "targets": [], "errors": []}
    try:
        manifest = load_json(repo / args.manifest)
        disposition_data = load_json(repo / args.dispositions)
        dispositions = disposition_data.get("survivors", {})
        if not isinstance(dispositions, dict):
            raise ValueError("survivors.json must contain a survivors mapping")
        patterns = disposition_data.get("patterns", [])
        if not isinstance(patterns, list) or any(
            not isinstance(rule, dict) or not rule.get("pattern") for rule in patterns
        ):
            raise ValueError("survivors.json patterns must be objects with a pattern")
        dispositions["__patterns__"] = patterns
        visibility = worktree_visibility_warning(uncommitted_paths(repo))
        if visibility:
            report.setdefault("warnings", []).append(visibility)
            print(f"mutation: {visibility}", file=sys.stderr, flush=True)
        changed = changed_paths(repo, args.base) if args.mode == "changed" else set()
        targets, missing = select_targets(manifest, args.mode, changed)
        runtime_costs = manifest.get("runtime_costs", {})
        worker_seconds = runtime_costs.get("worker_seconds", {}) \
            if isinstance(runtime_costs, dict) else {}
        if not isinstance(worker_seconds, dict):
            raise ValueError("mutation manifest runtime_costs.worker_seconds must be a mapping")
        targets = [
            {**target, "estimated_worker_seconds": worker_seconds.get(target["path"])}
            for target in targets
        ]
        floor_waivers = manifest.get("floor_waivers", {})
        if not isinstance(floor_waivers, dict):
            raise ValueError("mutation manifest floor_waivers must be a mapping")
        known_paths = {target["path"] for target in manifest["targets"]}
        unknown_waivers = sorted(set(floor_waivers) - known_paths)
        if unknown_waivers:
            raise ValueError(
                "floor waivers name unregistered targets: " + ", ".join(unknown_waivers)
            )
        targets = [
            {**target, "floor_waiver": floor_waivers.get(target["path"])}
            for target in targets
        ]
        absent = sorted(target["path"] for target in manifest["targets"]
                        if not (repo / target["path"]).is_file())
        if absent:
            raise ValueError("mutation targets do not exist: " + ", ".join(absent))
        if args.target:
            known = {target["path"] for target in targets}
            unknown = sorted(set(args.target) - known)
            if unknown:
                raise ValueError("requested mutation target is unavailable: " + ", ".join(unknown))
            targets = [target for target in targets if target["path"] in set(args.target)]
        if args.newly_critical_only:
            promoted = newly_critical_targets(repo, args.base, manifest)
            report["newly_critical"] = promoted
            if not promoted:
                # The common case: the manifest is untouched, so there is nothing to prove and
                # this costs one `git show`. Exit clean BEFORE any session work.
                report["checked"] = []
                print(json.dumps(report, indent=2, sort_keys=True))
                return 0
            known = {target["path"] for target in targets}
            missing_promoted = sorted(set(promoted) - known)
            if missing_promoted:
                raise ValueError(
                    "targets promoted to critical but absent from the selectable set: "
                    + ", ".join(missing_promoted))
            targets = [target for target in targets if target["path"] in set(promoted)]
        if args.critical_only:
            targets = [target for target in targets if target.get("critical")]
            if not targets:
                raise ValueError("--critical-only selected no targets: the manifest declares none")
        if args.mode == "full" or args.shard_count != 1 or args.shard_index != 0:
            complete_selection = list(targets)
            targets = select_shard(complete_selection, args.shard_count, args.shard_index)
            if not targets and args.mode == "full":
                raise ValueError(
                    f"mutation shard {args.shard_index}/{args.shard_count} selected no targets"
                )
            if args.mode == "full":
                report["manifest_sha256"] = manifest_identity(manifest)
            report["shard"] = {
                "count": args.shard_count,
                "index": args.shard_index,
                "manifest_target_count": len(complete_selection),
                "selected_paths": [target["path"] for target in targets],
            }
        if missing:
            report["errors"].append(
                "changed production modules lack mutation targets: " + ", ".join(missing)
            )
        if args.check_targets_only:
            # Deliberately BEFORE the --jobs/--dump validation and any session work: this mode
            # exists to be cheap. select_targets has already validated manifest shape (paths are
            # unique .py, targets non-empty), and `missing` is populated above.
            report["checked"] = sorted(target["path"] for target in targets)
            print(json.dumps(report, indent=2, sort_keys=True))
            if report["errors"]:
                for message in report["errors"]:
                    print(f"FAIL: {message}", file=sys.stderr)
                return 1
            print(f"mutation targets OK: {len(targets)} selected, every changed production "
                  f"module is registered", file=sys.stderr)
            return 0

        if args.preflight_only and args.dump:
            raise ValueError("--preflight-only cannot be combined with --dump")
        if args.preflight_only and (args.shard_count != 1 or args.shard_index != 0):
            raise ValueError("--preflight-only must cover the complete selected campaign")
        if args.dump and len(args.dump) != len(targets):
            raise ValueError("the number of --dump files must match selected targets")

        if args.jobs <= 0:
            raise ValueError("--jobs must be positive")

        if args.dump is None:
            cosmic = shlex.split(args.cosmic_ray)
            if not cosmic:
                raise ValueError("--cosmic-ray command cannot be empty")
            if args.preflight_only:
                preflight = preflight_baselines(repo, cosmic, artifacts, targets)
                proof = {
                    "status": "passed",
                    "manifest_sha256": manifest_identity(manifest),
                    "selected_paths": [target["path"] for target in targets],
                    **preflight,
                }
                (artifacts / "baseline-proof.json").write_text(
                    json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                print(json.dumps(proof, indent=2, sort_keys=True))
                return 0
            report["budget"] = validate_campaign_budget(
                targets, args.deadline_seconds, workers=args.jobs,
            )
            if args.baseline_proof:
                report["baseline_preflight"] = validate_baseline_proof(
                    args.baseline_proof, manifest_identity(manifest), targets,
                )
            else:
                report["baseline_preflight"] = preflight_baselines(
                    repo, cosmic, artifacts, targets,
                )

        def run_target(index_target: tuple[int, dict]) -> dict:
            index, target = index_target
            # Resolved BEFORE the session is built: the same scope drives both the generation-time
            # skip (so out-of-scope mutants are never executed) and the scoring-time filter.
            scope = None
            if args.mode == "changed" and not args.no_diff_scope:
                scope = changed_lines(repo, args.base, target["path"])
            dump = args.dump[index] if args.dump else None
            if dump is None:
                slug = target["path"].replace("/", "-").removesuffix(".py")
                target_dir = artifacts / slug
                target_dir.mkdir(parents=True, exist_ok=True)
                config_path = target_dir / "cosmic-ray.toml"
                session_path = target_dir / "session.sqlite"
                dump = target_dir / "results.jsonl"
                deadline = int(target.get("session_deadline_seconds", 3600))
                cosmic = shlex.split(args.cosmic_ray)
                config_path.write_text(cosmic_config(target), encoding="utf-8")
                # The shared baseline prerequisite was validated once per effective command before
                # any target started.  This checkout is exclusively for mutation initialization
                # and execution; repeating baseline here would restore the N-target amplification
                # that preflight exists to prevent.
                with isolated_checkout(repo) as checkout:
                    env = mutation_environment(checkout)
                    run_command(cosmic + ["init", str(config_path), str(session_path)], checkout,
                                deadline, env=env)
                    if scope is not None:
                        skipped, kept = skip_out_of_scope(session_path, scope)
                        print(f"{target['path']}: diff-scoped — {kept} mutant(s) to run, "
                              f"{skipped} skipped outside the diff", file=sys.stderr)
                    if args.jobs == 1:
                        run_command(cosmic + ["exec", str(config_path), str(session_path)],
                                    checkout, deadline, env=env)
                    else:
                        with cosmic_http_workers(repo, cosmic, args.jobs) as worker_urls:
                            config_path.write_text(
                                cosmic_config(target, worker_urls), encoding="utf-8")
                            run_command([
                                sys.executable, str(repo / "ci/cosmic_http_exec.py"),
                                str(config_path), str(session_path),
                            ], checkout, deadline, env=env)
                    dump_session(session_path, dump)
            annotation_unions = postponed_union_positions(repo / target["path"])
            exempt = coverage_exempt_lines(repo / target["path"])
            # A changed-mode target can legitimately contain no mutatable operator in the changed
            # lines (a release-version constant is the common case).  score_target already treats
            # an empty *diff-scoped* denominator as not applicable; let that explicit scope reach
            # the scorer.  Full campaigns remain fail-closed on an empty session, and malformed or
            # missing dumps still fail in parse_dump regardless of scope.
            rows = parse_dump(dump, allow_empty=True) if scope is not None else parse_dump(dump)
            return score_target(target, rows, dispositions, annotation_unions,
                                exempt, scope)

        # Per-target isolation. A comprehension here meant one failing target aborted the whole
        # campaign and `report["targets"]` was never assigned -- hours of completed work discarded,
        # reported as a null score that reads exactly like "nothing to measure".
        #
        # The same lesson, one level up (okengine#599): the SUMMARY was only written after every
        # target finished, so a campaign killed by an outer `timeout` published nothing at all --
        # the scheduled full run hit its 12h ceiling four nights running and left a 9.6 KB trace as
        # its only artifact. Twelve hours of measurement, discarded for want of a flush. So the
        # report is persisted after EVERY target, and a SIGTERM writes it before exiting: whatever
        # was measured survives the thing that stops the campaign.
        results: list[dict] = []
        unreached: list[str] = []
        started = time.monotonic()
        emit_progress(
            "shard_start",
            campaign_deadline_seconds=args.deadline_seconds,
            manifest_sha256=manifest_identity(manifest),
            selected_target_count=len(targets),
            shard_count=args.shard_count,
            shard_index=args.shard_index,
            workers=args.jobs,
        )

        def persist() -> None:
            snapshot = dict(report, targets=results,
                            not_measured=sorted(unreached),
                            elapsed_seconds=round(time.monotonic() - started, 1))
            snapshot["overall"] = aggregate(results)
            snapshot["critical"] = aggregate(results, critical_only=True)
            (artifacts / "summary.json").write_text(
                json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        def on_term(_signum, _frame):
            # `timeout --signal=TERM --kill-after=10s` gives exactly this window. Use it to
            # publish rather than to die quietly.
            #
            # os._exit, not sys.exit: SystemExit unwinds through the loop, which then ran one more
            # `persist()` and overwrote this snapshot -- the published report counted a target as
            # both measured AND not-measured, and the process stayed alive long enough for the
            # kill-after SIGKILL. Leaving immediately makes the handler the last writer.
            done = len(results)
            unreached.extend(target["path"] for target in targets[done:]
                             if target["path"] not in unreached)
            persist()
            emit_progress(
                "campaign_terminated",
                errored_target_count=sum(bool(result["infrastructure_errors"])
                                         for result in results),
                measured_target_count=done,
                unreached_target_count=len(unreached),
            )
            print(f"mutation gate: terminated after {done}/{len(targets)} target(s); "
                  f"partial results written to {artifacts / 'summary.json'}", file=sys.stderr)
            sys.stderr.flush()
            os._exit(143)

        signal.signal(signal.SIGTERM, on_term)
        for item in enumerate(targets):
            index, target = item
            if args.deadline_seconds and time.monotonic() - started >= args.deadline_seconds:
                # Stop STARTING work rather than being stopped mid-target. The campaign decides,
                # and says which targets it did not reach -- an unmeasured target is unknown, not
                # passing, and compliance_errors treats it that way.
                unreached.extend(remaining["path"] for remaining in targets[index:])
                break
            target_started = time.monotonic()
            emit_progress(
                "target_start",
                critical=bool(target.get("critical")),
                path=target["path"],
                target_count=len(targets),
                target_deadline_seconds=int(target.get("session_deadline_seconds", 3600)),
                target_index=index + 1,
            )
            with target_heartbeats(target["path"], target_started):
                try:
                    result = run_target(item)
                except (ValueError, RuntimeError) as exc:
                    result = unrunnable_target(target, str(exc))
            result["elapsed_seconds"] = round(time.monotonic() - target_started, 1)
            results.append(result)
            persist()
            emit_progress(
                "target_complete",
                completed_target_count=len(results),
                elapsed_seconds=result["elapsed_seconds"],
                killed=result["killed"],
                outcome=("error" if result["infrastructure_errors"] else "measured"),
                path=result["path"],
                score=result["score"],
                selected_target_count=len(targets),
                survived=result["survived"],
                total=result["total"],
            )
        report["targets"] = results
        report["not_measured"] = sorted(unreached)
        report["elapsed_seconds"] = round(time.monotonic() - started, 1)
    except (ValueError, RuntimeError) as exc:
        report["errors"].append(str(exc))
    report["overall"] = aggregate(report["targets"])
    report["critical"] = aggregate(report["targets"], critical_only=True)
    floors = manifest.get("floors", {}) if "manifest" in locals() else {}
    report["campaign_errors"] = list(report["errors"])
    report["errors"] = compliance_errors(
        report, float(floors.get("overall", 80)), float(floors.get("critical", 90)),
    )
    summary = artifacts / "summary.json"
    summary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_junit(artifacts / "junit.xml", report)
    print(json.dumps({"overall": report["overall"], "critical": report["critical"],
                      "errors": report["errors"]}, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
