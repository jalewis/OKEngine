"""Verified per-item completion receipts for bounded model cron runs."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

TERMINAL = {"accepted", "merged", "updated", "rejected-out-of-scope",
            "insufficient-evidence", "duplicate", "deferred-for-review", "failed"}
LEGACY_TERMINAL = {"skipped": "rejected-out-of-scope",
                   "rejected": "insufficient-evidence",
                   "deferred": "deferred-for-review"}
RETRYABLE = {"insufficient-evidence", "failed", "deferred-for-review"}
WRITE_DISPOSITIONS = {"accepted", "merged", "updated"}
_BLOCK = re.compile(
    r"```okengine-receipt[ \t]*\n\s*(\{.*?\})\s*```[ \t]*(?=\n|$)", re.S)
_FENCE = re.compile(r"```([^\n`]*)\n?(.*?)```", re.S)
_CANONICAL_OPEN = re.compile(r"```okengine-receipt[ \t]*(?:\n|$)")


def verify_run_completion(job: dict, started_at, wiki: Path) -> list[dict]:
    """Read back the governed writes promised by a whole-run contract."""
    contract = job.get("output_contract") or {}
    if contract.get("completion") != "run" or job.get("no_agent") is True:
        return []
    writes = job.get("_okengine_executed_writes") or []
    if not isinstance(writes, list) or not writes:
        raise ReceiptError("run completion requires at least one verified write; executed writes=0")
    root = wiki.resolve()
    verified: list[dict] = []
    for raw in writes:
        if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
            raise ReceiptError("run completion contains a malformed executed-write record")
        logical = raw["path"].removeprefix("wiki/").lstrip("/")
        target = (root / logical).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ReceiptError(f"executed write escapes wiki root: {raw['path']}") from exc
        if not target.is_file():
            raise ReceiptError(f"executed write failed read-back: {logical}")
        verified.append({**raw, "path": logical,
                         "sha256": "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()})
    required = contract.get("required_write_path")
    if required:
        # Daily lane names follow the deployment's wall-clock date.  Cron-plus
        # records starts in UTC, so convert before expanding a date placeholder
        # or late-evening runs west of UTC demand tomorrow's artifact.
        local_date = started_at.astimezone().strftime("%Y-%m-%d")
        expected = str(required).replace("{date}", local_date)
        expected = expected.removeprefix("wiki/").lstrip("/")
        candidates = {str(item["path"]).removesuffix(".md") for item in verified}
        if expected.removesuffix(".md") not in candidates:
            raise ReceiptError(
                f"required write was not executed: {expected} (verified: {sorted(candidates)})")
    return verified

# --- narrated tool calls (okengine#477) -------------------------------------
# A model whose tool calls are not parsed back into the API's `tool_calls` field
# emits them as PROSE in the final response. Nothing executes, yet the run looks
# successful: fluent output, finish_reason=stop, and — when the model also
# guesses a plausible receipt — a receipt that validates. Observed on
# qwen3-coder:30b: ~40 correct calls narrated in one turn, 0 executed, lane
# recorded complete. The wrong-tag shapes below are the model answering in the
# INPUT tag (<tools>, which carries the signatures) or emitting a stray closing
# tag, instead of a parsed call.
_NARRATED_TAG = re.compile(r"</?tool_call>|<tools>\s*\{|<function=|<parameter=")
# A bare call object: both "name" and "arguments" at the top level of one blob.
# Receipt items carry key/disposition/writes/reason and never this pair, so the
# shape does not collide with a legitimate receipt.
_NARRATED_JSON = re.compile(
    r'\{[^{}]*"name"\s*:\s*"[^"]+"[^{}]*"arguments"\s*:\s*\{', re.S)


def detect_no_executed_tool_calls(job: dict, selection: dict) -> list[str]:
    """Reject a receipt from a run that executed ZERO tool calls against real work.

    The stronger companion to ``detect_narrated_tool_calls``: it catches a run whose
    response contains no narration tell at all, yet did nothing — e.g. a truncated
    prompt that left the model reporting every item ``skipped`` without reading one
    (okengine#476, where 30 items were consumed that way).

    The count is published onto the job dict by the carried patch
    ``13-cron-executed-tool-calls.patch``; cron-plus hands ``run_job`` the same dict
    it later hands us. **Absent key means an unpatched runtime** — degrade to
    silence rather than failing every lane on an older image.
    """
    raw = job.get("_okengine_executed_tool_calls")
    if raw is None:
        return []                      # producer not deployed yet — do not guess
    try:
        executed = int(raw)
    except (TypeError, ValueError):
        return []
    if executed > 0:
        return []
    selected = selection.get("selected") or []
    if not selected:
        return []                      # nothing to do; doing nothing is correct
    return [f"run executed 0 tool calls against {len(selected)} selected item(s) — "
            "the receipt reports work that was never performed; retrying, not recording"]


def detect_narrated_tool_calls(response: str) -> list[str]:
    """Return errors when the final response CONTAINS tool calls instead of making them.

    Deliberately evidence-based rather than count-based: a zero-tool-call check
    would be stronger but needs the executed-call count threaded out of
    ``run_job``, which does not currently return it (okengine#477 follow-up).
    This catches the narrated-call class from the response text alone.
    """
    text = response or ""
    if not text.strip():
        return []
    # Never inspect inside the canonical receipt: its own content is not a call.
    outside = _BLOCK.sub("", text)
    tags = len(_NARRATED_TAG.findall(outside))
    blobs = len(_NARRATED_JSON.findall(outside))
    if not tags and not blobs:
        return []
    detail = []
    if tags:
        detail.append(f"{tags} tool-call tag(s)")
    if blobs:
        detail.append(f"{blobs} bare call object(s)")
    return ["response narrates tool calls instead of making them "
            f"({', '.join(detail)}) — the model's calls were not executed; "
            "this run did no work and must be retried, not recorded"]


def _has_positive_execution_telemetry(job: dict) -> bool:
    """Return true only when the patched runtime proves at least one tool turn ran."""
    try:
        return int(job.get("_okengine_executed_tool_calls")) > 0
    except (TypeError, ValueError):
        return False


class ReceiptError(ValueError):
    pass


def digest_items(keys: list[str]) -> str:
    raw = json.dumps(keys, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def canonical_disposition(value: object) -> str | None:
    """Return the durable product disposition, accepting legacy receipts during migration."""
    if value in TERMINAL:
        return str(value)
    return LEGACY_TERMINAL.get(str(value))


def _load_object(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReceiptError(f"invalid receipt JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ReceiptError("receipt must be an object")
    return value


def _matches_runner_identity(value: dict, expected: dict) -> bool:
    if any(value.get(field) != expected.get(field) for field in
           ("lane_id", "contract_digest", "input_digest")):
        return False
    items = value.get("items")
    if not isinstance(items, list):
        return False
    keys = [item.get("key") for item in items if isinstance(item, dict)]
    selected = expected.get("selected")
    return (isinstance(selected, list) and len(keys) == len(items) == len(selected)
            and len(keys) == len(set(keys)) and set(keys) == set(selected))


def parse_response_details(response: str, expected: dict | None = None) -> tuple[dict, str]:
    """Parse a canonical receipt or conservatively recover one JSON candidate.

    Recovery is deliberately identity-gated: cosmetic model formatting can be
    normalized, but selection identity and item accounting cannot.
    """
    text = response or ""
    canonical = _BLOCK.findall(text)
    if canonical:
        if len(canonical) != 1:
            raise ReceiptError("multiple okengine-receipt JSON blocks")
        return _load_object(canonical[0]), "canonical"
    if expected is None:
        raise ReceiptError("missing okengine-receipt JSON block")

    # Some models emit a structurally complete receipt but omit only the final
    # Markdown fence. JSON supplies its own unambiguous boundary; accept that
    # boundary only when the rest of the response is whitespace and identity
    # matches in full. A truncated object or trailing payload still fails.
    openings = list(_CANONICAL_OPEN.finditer(text))
    if openings:
        recovered = []
        decoder = json.JSONDecoder()
        for opening in openings:
            tail = text[opening.end():].lstrip()
            try:
                value, end = decoder.raw_decode(tail)
            except json.JSONDecodeError:
                continue
            if (not tail[end:].strip() and isinstance(value, dict)
                    and _matches_runner_identity(value, expected)):
                recovered.append(value)
        if len(recovered) == 1 and len(openings) == 1:
            return recovered[0], "recovered-unterminated-fence"
        if len(recovered) > 1 or len(openings) > 1:
            raise ReceiptError("multiple unterminated okengine-receipt candidates")
        raise ReceiptError("invalid unterminated okengine-receipt JSON block")

    candidates = []
    for label, body in _FENCE.findall(text):
        if label.strip().lower() not in {"", "json"}:
            continue
        try:
            value = json.loads(body.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and _matches_runner_identity(value, expected):
            candidates.append(value)
    stripped = text.strip()
    if not candidates and stripped.startswith("{") and stripped.endswith("}"):
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict) and _matches_runner_identity(value, expected):
            candidates.append(value)
    if len(candidates) != 1:
        if len(candidates) > 1:
            raise ReceiptError("multiple identity-matching receipt JSON candidates")
        raise ReceiptError("missing okengine-receipt JSON block")
    return candidates[0], "recovered-json"


def parse_response(response: str, expected: dict | None = None) -> dict:
    return parse_response_details(response, expected)[0]


def load_selection(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"selection manifest unavailable: {exc}") from exc
    keys = value.get("selected") if isinstance(value, dict) else None
    if not isinstance(keys, list) or any(not isinstance(k, str) or not k for k in keys):
        raise ReceiptError("selection manifest selected must be a list of item keys")
    if len(keys) != len(set(keys)):
        raise ReceiptError("selection manifest contains duplicate item keys")
    return {"selected": keys, "input_digest": value.get("input_digest") or digest_items(keys),
            "lane_id": value.get("lane_id"), "contract_digest": value.get("contract_digest")}


def _resolve_sharded(target: Path, claimed: str, wiki: Path) -> tuple[Path | None, str | None]:
    """Find a page the write path SHARDED away from the location the model claimed.

    okengine#478. `write_server._partitioned_create_path` rewrites a created page to
    its schema-canonical shard — `entities/tuxbot-v3` lands at `entities/t/u/tuxbot-v3`.
    The model reports the path it ASKED for, so a literal readback of the claimed path
    finds nothing and the receipt is rejected as "accepted write does not exist" even
    though the write succeeded. Observed live: 14 correct entity writes recorded as a
    failed run, selection unconsumed, the whole run redone.

    Deliberately NOT a fuzzy search. The basename must match exactly, the hit must be
    inside the claimed namespace, and EXACTLY ONE candidate may match — so this cannot
    launder a wrong path into a passing receipt, and a duplicate slug across two shards
    (the okengine#54 class) reports as ambiguous instead of silently picking one.

    Kept dependency-free like the rest of this module: it does not import the engine's
    partition helper, because this file is staged into the cron-plus plugin and must
    run with the standard library alone.
    """
    rel = claimed.removeprefix("wiki/")
    parts = Path(rel).parts
    if len(parts) < 2:                       # no namespace to scope the search to
        return None, None
    namespace = wiki / parts[0]
    if not namespace.is_dir():
        return None, None
    names = {target.name}
    if not Path(rel).suffix:
        names.add(target.name + ".md")
    hits = [p for name in names for p in namespace.rglob(name) if p.is_file()]
    if not hits:
        return None, None
    if len(hits) > 1:
        listed = ", ".join(sorted(str(h.relative_to(wiki)) for h in hits)[:4])
        return None, (f"accepted write is ambiguous after sharding: {claimed} "
                      f"matches {len(hits)} pages ({listed})")
    return hits[0], None


def executed_writes(job: dict) -> list[dict] | None:
    """The writes this run actually performed, as reported by the write tools themselves.

    okengine#469/#478. Hermes patch 14 publishes `_okengine_executed_writes` onto the job
    dict: one entry per `mcp__okengine_write_*` tool result, carrying the CANONICAL path the
    write path returned (post-shard) rather than the flat path the model requested.

    Returns None when the key is absent, which is how an OLDER gateway image presents — every
    telemetry check must then stay silent rather than fail the run, exactly as patch 13's
    executed-call count does. An empty list means the run genuinely wrote nothing.
    """
    value = job.get("_okengine_executed_writes")
    if not isinstance(value, list):
        return None
    return [w for w in value if isinstance(w, dict) and isinstance(w.get("path"), str)]


def _telemetry_paths(job: dict) -> set[str] | None:
    writes = executed_writes(job)
    if writes is None:
        return None
    return {w["path"].removeprefix("wiki/") for w in writes}


def _readback(item: dict, wiki: Path, telemetry: set[str] | None = None
              ) -> tuple[list[str], list[dict]]:
    errors = []
    reconciliation = []
    paths = item.get("writes") or []
    if canonical_disposition(item.get("disposition")) in WRITE_DISPOSITIONS and not paths:
        return ["write disposition has no writes"], reconciliation
    for write in paths:
        if not isinstance(write, dict) or not isinstance(write.get("path"), str):
            errors.append("write record must contain path")
            continue
        target = (wiki / write["path"].removeprefix("wiki/")).resolve()
        try:
            target.relative_to(wiki.resolve())
        except ValueError:
            errors.append(f"write path escapes wiki: {write['path']}")
            continue
        if not target.is_file():
            # Ground truth first: if the run's own telemetry recorded a write whose
            # canonical path ends in this basename, that IS where the page went — no
            # scanning, no ambiguity. The shard scan below stays as the fallback for
            # gateways older than patch 14.
            resolved, ambiguous = None, None
            if telemetry:
                hits = {c for c in telemetry if Path(c).name == target.name}
                if len(hits) == 1:
                    candidate = (wiki / hits.pop()).resolve()
                    if candidate.is_file():
                        resolved = candidate
            if resolved is None:
                resolved, ambiguous = _resolve_sharded(target, write["path"], wiki)
            if ambiguous:
                errors.append(ambiguous)
                continue
            if resolved is None:
                errors.append(f"accepted write does not exist: {write['path']}")
                continue
            reconciliation.append({
                "path": write["path"],
                "observed_path": str(resolved.relative_to(wiki.resolve())),
                "classification": "receipt-path-sharded",
            })
            target = resolved
        # `telemetry` empty is NOT evidence the run wrote nothing — it is equally the
        # signature of a telemetry PARSE failure, and treating the two alike turned 28
        # real writes into "not performed by this run" on the canary. Only an entry-bearing
        # telemetry set may refuse a write; an empty one falls through to the existing
        # on-disk checks. Absence of evidence, not evidence of absence.
        if telemetry and canonical_disposition(item.get("disposition")) in WRITE_DISPOSITIONS:
            claimed_rel = str(target.relative_to(wiki.resolve()))
            if claimed_rel not in telemetry and not any(
                    Path(c).name == target.name for c in telemetry):
                # The page exists but THIS run did not write it — the receipt is claiming
                # credit for a pre-existing page. This is the anti-fabrication signal the
                # receipt was always meant to carry, now taken from telemetry instead of
                # the model's word (okengine#469).
                errors.append(
                    f"accepted write not performed by this run: {write['path']}")
                continue
        actual = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
        if write.get("sha256") != actual:
            errors.append(f"accepted write hash mismatch: {write['path']}")
            reconciliation.append({
                "path": write["path"],
                "claimed_sha256": write.get("sha256"),
                "observed_sha256": actual,
                "classification": "receipt-hash-mismatch",
            })
    return errors, reconciliation


def _effect_readback(item: dict, job: dict, wiki: Path) -> list[str]:
    """Verify accepted operations whose durable effect is not a target-page write."""
    key = str(item.get("key") or "")
    path, separator, action = key.rpartition("|")
    operations = ((job.get("output_contract") or {}).get("operations") or [])
    if separator and action == "quarantine-for-review" and "flag" in operations:
        queue = wiki / "_review-queue.md"
        if not queue.is_file():
            return ["accepted review flag has no review queue"]
        queue_text = queue.read_text(encoding="utf-8", errors="replace")
        candidates = {path, path.removesuffix(".md")}
        if not any(candidate and candidate in queue_text for candidate in candidates):
            return [f"accepted review flag is absent from review queue: {path}"]
        return []
    return ["accepted item has no writes"]


def validate(receipt: dict, selection: dict, job: dict, wiki: Path) -> dict:
    errors = []
    reconciliation = []
    telemetry = _telemetry_paths(job)
    if receipt.get("api") != 1:
        errors.append("receipt api must be 1")
    for field, expected in (("lane_id", job.get("id")),
                            ("contract_digest", job.get("output_contract_digest")),
                            ("input_digest", selection["input_digest"])):
        if receipt.get(field) != expected:
            errors.append(f"{field} does not match runner-owned value")
    if selection.get("lane_id") != job.get("id"):
        errors.append("selection lane_id does not match runner-owned value")
    if selection.get("contract_digest") != job.get("output_contract_digest"):
        errors.append("selection contract_digest does not match runner-owned value")
    selected = selection["selected"]
    identity_valid = not errors
    items = receipt.get("items")
    if not isinstance(items, list):
        items = []
        errors.append("items must be a list")
    by_key = {}
    verified = []
    duplicate_keys = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            errors.append("each item must contain a key")
            continue
        key = item["key"]
        if key in by_key:
            errors.append(f"selected item has multiple dispositions: {key}")
            duplicate_keys.add(key)
        by_key[key] = item
        disposition = canonical_disposition(item.get("disposition"))
        item_error_count = len(errors)
        if disposition is None:
            errors.append(f"invalid disposition for {key}: {item.get('disposition')!r}")
        if disposition not in WRITE_DISPOSITIONS and not str(item.get("reason") or "").strip():
            errors.append(f"{disposition} item requires a machine-verifiable reason: {key}")
        if disposition in WRITE_DISPOSITIONS:
            writes = item.get("writes") or []
            if writes:
                readback, observed = _readback(item, wiki, telemetry)
                reconciliation.extend({"key": key, **entry} for entry in observed)
            else:
                readback = _effect_readback(item, job, wiki)
            errors.extend(f"{key}: {e}" for e in readback)
        if (identity_valid and len(errors) == item_error_count
                and disposition in TERMINAL):
            verified.append(key)
    missing = [key for key in selected if key not in by_key]
    extra = [key for key in by_key if key not in set(selected)]
    if missing:
        errors.append("selected item(s) undisposed: " + ", ".join(missing))
    if extra:
        errors.append("receipt contains unselected item(s): " + ", ".join(extra))
    verified = [key for key in verified
                if key in set(selected) and key not in duplicate_keys]
    reconciliation = [entry for entry in reconciliation
                      if identity_valid and entry["key"] in set(selected)
                      and entry["key"] not in duplicate_keys]
    counts = {status: 0 for status in TERMINAL}
    for key in selected:
        status = canonical_disposition((by_key.get(key) or {}).get("disposition"))
        if status in counts:
            counts[status] += 1
    # Preserve the legacy reporting keys for callers during the vocabulary migration.
    # They are aliases only: canonical counters remain the source of truth and rollups
    # must not sum both forms.
    legacy_counts = {
        legacy: counts[canonical] for legacy, canonical in LEGACY_TERMINAL.items()
    }
    undisposed = len(missing)
    if errors or undisposed:
        state = "failed"
    elif counts["failed"]:
        state = "partial"
    elif counts["insufficient-evidence"] or counts["deferred-for-review"]:
        state = "degraded"
    else:
        state = "succeeded"
    return {"valid": not errors, "errors": errors, "state": state,
            "counts": {"selected": len(selected), **counts, **legacy_counts,
                       "undisposed": undisposed},
            "prompt_metrics": {
                **(job.get("prompt_metrics") or {}),
                "bytes_per_successful_disposition": (
                    (job.get("prompt_metrics") or {}).get("bytes") / counts["accepted"]
                    if counts["accepted"] and isinstance(
                        (job.get("prompt_metrics") or {}).get("bytes"), (int, float)) else None),
            },
            "reconciliation": reconciliation,
            "verified": verified,
            "retry": [key for key in selected
                      if canonical_disposition(
                          (by_key.get(key) or {}).get("disposition")) in RETRYABLE]}


def _key_path(key: str) -> str:
    """The vault-relative path a selection key identifies, without the `|<revision>` suffix."""
    return key.split("|", 1)[0].removeprefix("wiki/")


def _attribution_refs(key: str) -> tuple[str, ...]:
    """Strings that DETERMINISTICALLY tie a written page back to this selected input.

    okengine#469/#485. Attribution is never inferred from proximity or ordering — a page counts
    for an input only if it carries one of these, or IS that input's page (see `_attribute_writes`).
    Three lane families, three real edges, all already present in the data:

      entity lanes  key `2026/07/20/foo.md`      -> grounded page cites `sources/2026/07/20/foo`
      raw lanes     key `raw/indicators/x.md`    -> written source page lists it in `raw:` verbatim
      source lanes  key `wiki/sources/a/b.md`    -> the write IS that page (path identity)

    Returned refs cover the first two; path identity is checked separately because it needs no
    read of the file at all.
    """
    path = _key_path(key)
    stem = path[:-3] if path.endswith(".md") else path
    refs = [path]                       # raw lanes cite the key path verbatim
    if not stem.startswith("sources/"):
        refs.append(f"sources/{stem}")  # entity lanes cite the suffix-stripped source ref
    else:
        refs.append(stem)
    return tuple(dict.fromkeys(refs))


def _attribute_writes(selected: list[str], records: dict[str, dict],
                      wiki: Path) -> dict[str, list[dict]]:
    """Map each written page to the selected input(s) it cites.

    A page citing two selected sources is evidence for both — it is deliberately not a
    partition. A page citing none stays unattributed and is simply not claimed.
    """
    refs = {key: _attribution_refs(key) for key in selected}
    paths = {key: _key_path(key) for key in selected}
    out: dict[str, list[dict]] = {key: [] for key in selected}
    for rel, record in records.items():
        # PATH IDENTITY first — an update-in-place lane writes the very page it selected, so the
        # write needs no citation to be attributable and the file need not even be read.
        identity = [k for k, path in paths.items() if path == rel]
        if identity:
            for key in identity:
                out[key].append(record)
            continue
        try:
            text = (wiki / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for key, candidates in refs.items():
            if any(ref in text for ref in candidates):
                out[key].append(record)
    return out


def _changed_selected_writes(selected: list[str], wiki: Path) -> list[dict]:
    changed = []
    for key in selected:
        raw_path, sep, before = key.partition("|")
        if not sep or not before.startswith("sha256:"):
            continue
        rel = raw_path.removeprefix("wiki/")
        target = (wiki / rel).resolve()
        try:
            target.relative_to(wiki.resolve())
        except ValueError:
            continue
        if not target.is_file():
            continue
        observed = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
        if observed != before:
            changed.append({"path": rel, "operation": "updated"})
    return changed


def synthesize_receipt(job: dict, selection: dict, wiki: Path) -> dict | None:
    """Build a receipt from write telemetry when the model omitted one (okengine#469).

    The largest single cause of failed runs is a model that does the work correctly and
    then does not describe it -- 495 of 866 invalid receipts on one deployment. The
    description is now recoverable without asking: patch 14 records every executed write
    with the canonical path the write path itself returned, and `_readback` recomputes the
    hash from disk anyway, so the model's transcription was never the source of truth.

    Deliberately narrow. Returns None -- leaving the existing "missing receipt" failure in
    place -- unless ALL of:

      * telemetry is present (a pre-patch-14 gateway must not be read as "the run wrote
        nothing"). An explicit empty list is positive evidence that no governed write
        succeeded, so every selected item can honestly remain retryable as ``deferred``.
      * accepted writes are attributable to their selected input. With N selected inputs,
        inventing a mapping would manufacture accounting rather than recover it. When
        telemetry proves writes occurred but supplies no deterministic edge, all selected
        inputs are conservatively deferred: the durable writes remain observable in runner
        telemetry, while the receipt claims no completion and loses no selected work.
      * every telemetried path exists on disk

    A synthesised receipt still goes through `validate()` unchanged -- contract, namespaces,
    readback and hash all still apply. This recovers the ACCOUNTING, it does not bypass the
    checks, and `receipt_source` records that it happened so a synthesised run is never
    mistaken for a model-authored one.
    """
    selected = selection.get("selected") or []
    if not selected:
        return None
    telemetry = executed_writes(job)
    if telemetry is None:
        return None
    # Native file update tools do not publish MCP write telemetry. An
    # update-in-place selector can still make the durable effect
    # self-verifying by embedding the selected page's pre-write digest in its
    # key. Prefer this exact cryptographic attribution over any unrelated MCP
    # telemetry emitted during the same run.
    changed = _changed_selected_writes(selected, wiki)
    writes = changed or telemetry
    if not writes:
        return {
            "api": 1,
            "run_id": "telemetry",
            "lane_id": job.get("id"),
            "contract_digest": job.get("output_contract_digest"),
            "input_digest": selection["input_digest"],
            "items": [
                {
                    "key": key,
                    "disposition": "deferred-for-review",
                    "reason": "no successful governed write recorded by this run",
                }
                for key in selected
            ],
        }
    # Last write wins per path: a page created then updated in one run is ONE durable effect.
    by_path: dict[str, dict] = {}
    for w in writes:
        by_path[w["path"].removeprefix("wiki/")] = w
    records = {}
    for rel in sorted(by_path):
        target = (wiki / rel).resolve()
        if not target.is_file():
            return None
        records[rel] = {"path": f"wiki/{rel}",
                        "sha256": "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()}
    if not records:
        return None

    attributed = _attribute_writes(selected, records, wiki)
    if not any(attributed.values()):
        if len(selected) != 1:
            return {
                "api": 1,
                "run_id": "telemetry",
                "lane_id": job.get("id"),
                "contract_digest": job.get("output_contract_digest"),
                "input_digest": selection["input_digest"],
                "items": [
                    {
                        "key": key,
                        "disposition": "deferred-for-review",
                        "reason": (
                            "successful writes were recorded but could not be "
                            "attributed to this selected input"
                        ),
                    }
                    for key in selected
                ],
            }
        # ONE selected input and writes that cite nothing: attribution is not needed, because
        # there is only one thing they could belong to. This is the sole case where claiming
        # without an edge is a fact rather than a guess.
        attributed = {selected[0]: list(records.values())}

    items = []
    for key in selected:
        got = attributed.get(key) or []
        if got:
            items.append({"key": key, "disposition": "accepted", "writes": got})
        else:
            # DEFERRED, never "skipped". We know this run wrote nothing for this input;
            # we do NOT know whether it was considered and rejected or never reached.
            # `deferred-for-review` is retryable, so the item is re-offered rather than consumed on
            # an assumption — the difference between recovering accounting and inventing it.
            items.append({"key": key, "disposition": "deferred-for-review",
                          "reason": "no write recorded by this run for this input"})
    return {"api": 1,
            "run_id": "telemetry",
            "lane_id": job.get("id"),
            "contract_digest": job.get("output_contract_digest"),
            "input_digest": selection["input_digest"],
            "items": items}


def verify_response(job: dict, response: str, wiki: Path) -> tuple[dict, dict]:
    manifest = job.get("selection_manifest")
    if not isinstance(manifest, str) or not manifest:
        raise ReceiptError("per-item lane has no selection_manifest")
    selection = load_selection(Path(manifest))
    expected = {"lane_id": job.get("id"),
                "contract_digest": job.get("output_contract_digest"),
                "input_digest": selection["input_digest"],
                "selected": selection["selected"]}
    try:
        receipt, source = parse_response_details(response, expected)
    except ReceiptError:
        # The model did the work and did not describe it. Recover the accounting from
        # telemetry where attribution is a fact rather than a guess; otherwise re-raise
        # and fail exactly as before.
        receipt = synthesize_receipt(job, selection, wiki)
        if receipt is None:
            raise
        source = ("evidence-synthesized" if _changed_selected_writes(
            selection["selected"], wiki) else "telemetry-synthesized")
    if any(
        isinstance(item, dict) and canonical_disposition(item.get("disposition")) is None
        for item in receipt.get("items") or []
    ):
        recovered = synthesize_receipt(job, selection, wiki)
        if recovered is not None:
            receipt = recovered
            source = "telemetry-repaired-nonterminal-dispositions"
    normalized = _normalize_write_hashes(receipt, job, wiki)
    validation_job = job
    if source.startswith("evidence-"):
        validation_job = {**job, "_okengine_executed_writes": None}
    result = validate(receipt, selection, validation_job, wiki)
    if not result["valid"]:
        recovered = synthesize_receipt(job, selection, wiki)
        if recovered is not None:
            evidence = bool(_changed_selected_writes(selection["selected"], wiki))
            repair_job = ({**job, "_okengine_executed_writes": None}
                          if evidence else job)
            repaired = validate(recovered, selection, repair_job, wiki)
            if repaired["valid"]:
                receipt = recovered
                result = repaired
                source = "evidence-repaired-invalid-receipt"
    result["receipt_source"] = source
    result["normalized_write_hashes"] = normalized
    # Narrated calls invalidate a receipt when there is no positive execution
    # evidence. At the iteration limit Hermes can summarize already-executed calls
    # using tool-call markup; the runtime counter distinguishes that harmless
    # terminal summary from #477's zero-execution failure.
    narrated = ([] if _has_positive_execution_telemetry(job)
                else detect_narrated_tool_calls(response))
    unexecuted = detect_no_executed_tool_calls(job, selection)
    if narrated or unexecuted:
        result["errors"] = list(result.get("errors") or []) + narrated + unexecuted
        result["valid"] = False
        result["state"] = "failed"
        if narrated:
            result["narrated_tool_calls"] = True
        if unexecuted:
            result["no_executed_tool_calls"] = True
    if result["valid"]:
        _record_terminal_items(Path(manifest), receipt)
    return receipt, result


def _normalize_write_hashes(receipt: dict, job: dict, wiki: Path) -> list[dict]:
    """Replace model-copied hashes with authoritative immediate read-back hashes.

    Repair jobs already use write-time page preconditions. Their completion
    receipt is bookkeeping, so making the model reproduce a hash is needless
    free-form failure. This mode remains opt-in per lane.
    """
    if job.get("receipt_hash_mode") != "readback":
        return []
    normalized = []
    telemetry = _telemetry_paths(job)
    for item in receipt.get("items") or []:
        if (not isinstance(item, dict)
                or canonical_disposition(item.get("disposition")) not in WRITE_DISPOSITIONS):
            continue
        writes = item.get("writes") or []
        # Local models commonly compress {"path": "..."} to the path string itself.
        # In readback mode the runner owns the rest of the record, so this is an
        # unambiguous representation repair rather than trusting model-supplied data.
        if isinstance(writes, list):
            writes = [{"path": value} if isinstance(value, str) else value
                      for value in writes]
            item["writes"] = writes
        for write in writes:
            if not isinstance(write, dict) or not isinstance(write.get("path"), str):
                continue
            claimed_path = write["path"]
            target = (wiki / claimed_path.removeprefix("wiki/")).resolve()
            try:
                target.relative_to(wiki.resolve())
            except ValueError:
                continue
            if not target.is_file():
                resolved = None
                if telemetry:
                    if len(telemetry) == 1 and len(writes) == 1:
                        candidate = (wiki / next(iter(telemetry))).resolve()
                        if candidate.is_file():
                            resolved = candidate
                    claimed_names = {target.name}
                    if not Path(claimed_path.removeprefix("wiki/")).suffix:
                        claimed_names.add(target.name + ".md")
                    hits = {path for path in telemetry if Path(path).name in claimed_names}
                    if resolved is None and len(hits) == 1:
                        candidate = (wiki / hits.pop()).resolve()
                        if candidate.is_file():
                            resolved = candidate
                if resolved is None:
                    resolved, ambiguous = _resolve_sharded(target, claimed_path, wiki)
                    if ambiguous:
                        continue
                if resolved is None:
                    continue
                target = resolved
                write["path"] = "wiki/" + str(target.relative_to(wiki.resolve()))
            observed = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
            claimed = write.get("sha256")
            if claimed != observed or write["path"] != claimed_path:
                write["sha256"] = observed
                record = {
                    "key": item.get("key"),
                    "path": write["path"],
                    "claimed_sha256": claimed,
                    "observed_sha256": observed,
                }
                if write["path"] != claimed_path:
                    record["claimed_path"] = claimed_path
                normalized.append(record)
    return normalized


def _record_terminal_items(manifest: Path, receipt: dict) -> None:
    """Persist model-terminal non-write decisions so selectors do not loop on them.

    Accepted writes have their own durable page/read-back marker. Duplicate and
    skipped inputs do not necessarily produce a write, so the runner owns their
    durable completion marker after the complete receipt has validated.
    """
    path = manifest.with_name(manifest.stem + ".completed.json")
    try:
        current = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    for item in receipt.get("items") or []:
        if not isinstance(item, dict):
            continue
        if canonical_disposition(item.get("disposition")) not in {
                "duplicate", "rejected-out-of-scope"}:
            continue
        key = item.get("key")
        if isinstance(key, str) and key:
            current[key] = {
                "disposition": item["disposition"],
                "reason": str(item.get("reason") or ""),
            }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        temporary.replace(path)
    except OSError as exc:
        raise ReceiptError(f"cannot persist completion ledger: {exc}") from exc
