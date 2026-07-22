"""Verified per-item completion receipts for bounded model cron runs."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

TERMINAL = {"accepted", "duplicate", "skipped", "rejected", "failed", "deferred"}
RETRYABLE = {"rejected", "failed", "deferred"}
_BLOCK = re.compile(
    r"```okengine-receipt[ \t]*\n\s*(\{.*?\})\s*```[ \t]*(?=\n|$)", re.S)
_FENCE = re.compile(r"```([^\n`]*)\n?(.*?)```", re.S)
_CANONICAL_OPEN = re.compile(r"```okengine-receipt[ \t]*(?:\n|$)")


class ReceiptError(ValueError):
    pass


def digest_items(keys: list[str]) -> str:
    raw = json.dumps(keys, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


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


def _readback(item: dict, wiki: Path) -> list[str]:
    errors = []
    paths = item.get("writes") or []
    if item.get("disposition") == "accepted" and not paths:
        return ["accepted item has no writes"]
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
            errors.append(f"accepted write does not exist: {write['path']}")
            continue
        actual = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
        if write.get("sha256") != actual:
            errors.append(f"accepted write hash mismatch: {write['path']}")
    return errors


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
    items = receipt.get("items")
    if not isinstance(items, list):
        items = []
        errors.append("items must be a list")
    by_key = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            errors.append("each item must contain a key")
            continue
        key = item["key"]
        if key in by_key:
            errors.append(f"selected item has multiple dispositions: {key}")
        by_key[key] = item
        disposition = item.get("disposition")
        if disposition not in TERMINAL:
            errors.append(f"invalid disposition for {key}: {disposition!r}")
        if disposition in {"duplicate", "skipped"} and not str(item.get("reason") or "").strip():
            errors.append(f"{disposition} item requires a machine-verifiable reason: {key}")
        if disposition == "accepted":
            writes = item.get("writes") or []
            readback = _readback(item, wiki) if writes else _effect_readback(item, job, wiki)
            errors.extend(f"{key}: {e}" for e in readback)
    missing = [key for key in selected if key not in by_key]
    extra = [key for key in by_key if key not in set(selected)]
    if missing:
        errors.append("selected item(s) undisposed: " + ", ".join(missing))
    if extra:
        errors.append("receipt contains unselected item(s): " + ", ".join(extra))
    counts = {status: 0 for status in TERMINAL}
    for key in selected:
        status = (by_key.get(key) or {}).get("disposition")
        if status in counts:
            counts[status] += 1
    undisposed = len(missing)
    if errors or undisposed:
        state = "failed"
    elif counts["failed"]:
        state = "partial"
    elif counts["rejected"] or counts["deferred"]:
        state = "degraded"
    else:
        state = "succeeded"
    return {"valid": not errors, "errors": errors, "state": state,
            "counts": {"selected": len(selected), **counts, "undisposed": undisposed},
            "retry": [key for key in selected
                      if (by_key.get(key) or {}).get("disposition") in RETRYABLE]}


def verify_response(job: dict, response: str, wiki: Path) -> tuple[dict, dict]:
    manifest = job.get("selection_manifest")
    if not isinstance(manifest, str) or not manifest:
        raise ReceiptError("per-item lane has no selection_manifest")
    selection = load_selection(Path(manifest))
    expected = {"lane_id": job.get("id"),
                "contract_digest": job.get("output_contract_digest"),
                "input_digest": selection["input_digest"],
                "selected": selection["selected"]}
    receipt, source = parse_response_details(response, expected)
    result = validate(receipt, selection, job, wiki)
    result["receipt_source"] = source
    return receipt, result
