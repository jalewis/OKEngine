#!/usr/bin/env python3
"""Deterministically repair broken-YAML frontmatter in vault pages.

Two repair classes, both STRICTLY GATED: a fix is written only if the
resulting frontmatter parses under yaml.safe_load AND still contains a
`type:` key. Anything that doesn't cleanly repair is left untouched
(re-quarantined) — we never make a broken file worse.

Class A — body-bleed (missing/misplaced closing `---`):
  An agent wrote frontmatter without a closing delimiter, so body markdown
  (`# Heading`, `**bold**`, prose) bled into the YAML block and tripped the
  parser (a `*`/`**` line reads as a YAML alias). Fix: find where the real
  frontmatter ends (last contiguous YAML-shaped line) and insert `---`
  there, pushing the heading/body back out.

Class B — common in-frontmatter syntax slips:
  - unclosed inline flow list:  `field_a: [item-one`  → close it
  - stray duplicate closing of a flow list, trailing comma, etc.
  (Only the narrow, safe, verifiable patterns — everything else is left.)

Idempotent (already-valid files are skipped). Skips backup/_archived.
Skips+logs host-owned files. Reports fixed vs still-broken counts.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"

_FM_OPEN_RE = re.compile(r"\A---[ \t]*\n")
_SKIP_SUBSTRINGS = (".bak", "_archived/", ".was-broken", ".restored",
                    ".corrupt", ".recovered", ".backup", ".applied")

# A line that is plausibly part of YAML frontmatter.
_YAML_KEY_RE = re.compile(r"^[A-Za-z_][\w-]*:(\s|$)")
_YAML_LIST_RE = re.compile(r"^[ \t]+-[ \t]")
_YAML_CONT_RE = re.compile(r"^[ \t]+\S")          # indented continuation/scalar
_COMMENT_RE = re.compile(r"^[ \t]*#")
_BLANK_RE = re.compile(r"^[ \t]*$")
# Strong body-start signals (definitely NOT frontmatter).
_BODY_BOLD_RE = re.compile(r"^\*\*|^[ \t]*\*\*")
_BODY_H_RE = re.compile(r"^#{1,6}\s")             # markdown heading
_BODY_PROSE_RE = re.compile(r"^[A-Za-z0-9>\[(]")  # line starting with prose/link/quote, no key

# `cat -n` / Read-tool line-number prefixes an agent wrote back into a file:
#   "     12|..." (number+pipe) or a "|---" mangled opening fence. These break
#   _FM_OPEN_RE so the file is wrongly treated as "no frontmatter" and skipped.
_CATN_PREFIX_RE = re.compile(r"^\s*\d+\|")          # "     12|"  (never a legit line)
_PIPE_FENCE_RE = re.compile(r"^\|+-{3,}[ \t]*$")    # "|---" / "||---"  mangled fence


def _is_skippable(path: Path) -> bool:
    return any(frag in str(path) for frag in _SKIP_SUBSTRINGS)


def parses_with_type(fm_text: str) -> bool:
    try:
        data = yaml.safe_load(fm_text)
    except Exception:
        return False
    return isinstance(data, dict) and "type" in data


def _is_yaml_line(line: str) -> bool:
    return bool(
        _YAML_KEY_RE.match(line) or _YAML_LIST_RE.match(line)
        or _BLANK_RE.match(line) or _YAML_CONT_RE.match(line)
        or _COMMENT_RE.match(line)
    )


def _is_strong_body(line: str) -> bool:
    if _BODY_BOLD_RE.match(line) or _BODY_H_RE.match(line):
        return True
    # prose line WITHOUT a leading key: (a key would have matched _YAML_KEY_RE)
    if _BODY_PROSE_RE.match(line) and not _YAML_KEY_RE.match(line):
        return True
    return False


def repair_body_bleed(text: str) -> str | None:
    """Class A. Return repaired text or None if not applicable / unsafe."""
    mo = _FM_OPEN_RE.match(text)
    if not mo:
        return None
    after_open = text[mo.end():]
    lines = after_open.splitlines(keepends=True)

    # Find the first STRONG body line. Everything before it (minus trailing
    # heading/blank) is the true frontmatter.
    body_idx = None
    for i, ln in enumerate(lines):
        s = ln.rstrip("\n")
        # an existing closing delimiter means frontmatter was fine here —
        # but we only got called because parse failed, so a `---` we hit
        # before any strong-body line is the (wrong) close the regex used.
        if s.strip() == "---":
            return None  # there IS a close; this isn't a pure body-bleed
        if _is_strong_body(s):
            body_idx = i
            break
        if not _is_yaml_line(s):
            # some other non-YAML, non-strong-body line — too ambiguous
            return None
    if body_idx is None:
        return None

    # Walk back over heading/blank lines so a `# Title` goes to the body.
    end = body_idx
    while end > 0:
        prev = lines[end - 1].rstrip("\n")
        if _BODY_H_RE.match(prev) or _BLANK_RE.match(prev) or _COMMENT_RE.match(prev):
            end -= 1
        else:
            break

    fm_lines = lines[:end]
    body_lines = lines[end:]
    fm_text = "".join(fm_lines)
    if not parses_with_type(fm_text):
        return None  # gate: only if it actually parses now

    fm_block = fm_text if fm_text.endswith("\n") else fm_text + "\n"
    body_block = "".join(body_lines)
    return "---\n" + fm_block + "---\n" + ("\n" if not body_block.startswith("\n") else "") + body_block


def repair_inline_flow(text: str) -> str | None:
    """Class B. Close a single unclosed inline flow list, e.g.
    `field_a: [item-one`  → `field_a: [item-one]`.
    Only when that single edit makes the frontmatter parse."""
    m = re.match(r"\A(---[ \t]*\n)(.*?\n)(---[ \t]*(?:\n|\Z))", text, re.S)
    if not m:
        return None
    fm = m.group(2)
    # find a line with `key: [ ... ` that has no closing ] on the same line
    new_fm = None
    for ln in fm.splitlines():
        if re.match(r"^[A-Za-z_][\w-]*:[ \t]*\[[^\]]*$", ln):
            candidate = fm.replace(ln, ln + "]", 1)
            if parses_with_type(candidate):
                new_fm = candidate
                break
    if new_fm is None:
        return None
    return m.group(1) + new_fm + m.group(3) + text[m.end():]


def repair_trailing_garbage(text: str) -> str | None:
    """Class C. Strip stray trailing quote/bracket after a valid inline list,
    e.g. `field_a: [item-one, item-two]"]` → `[...]` or
    `field_a: [item-one]"` → `[...]`. Only if it then parses."""
    m = re.match(r"\A(---[ \t]*\n)(.*?\n)(---[ \t]*(?:\n|\Z))", text, re.S)
    if not m:
        return None
    fm = m.group(2)
    # a line ending in `]` followed by stray `"` and/or `]` chars
    new_fm = re.sub(r'(:[ \t]*\[[^\]\n]*\])["\]]+[ \t]*$', r"\1", fm, flags=re.MULTILINE)
    if new_fm == fm or not parses_with_type(new_fm):
        return None
    return m.group(1) + new_fm + m.group(3) + text[m.end():]


def repair_malformed_close(text: str) -> str | None:
    """Class D. Frontmatter whose closing delimiter is typo'd (e.g. `|---`,
    `----`, `-- -`). Find the first delimiter-ish line after the opening and
    normalize it to `---`, only if the preceding block then parses."""
    if not _FM_OPEN_RE.match(text):
        return None
    # already has a clean close? then not our case
    if re.match(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", text, re.S):
        try:
            mm = re.match(r"\A---[ \t]*\n(.*?)\n---", text, re.S)
            yaml.safe_load(mm.group(1))
            return None  # clean close already parses
        except Exception:
            pass
    lines = text.splitlines(keepends=True)
    for i in range(1, len(lines)):
        s = lines[i].rstrip("\n")
        if re.match(r"^\|?[ \t]*-{2,}[ \t-]*$", s) and s.strip() != "---":
            fm = "".join(lines[1:i])
            if parses_with_type(fm):
                rebuilt = lines[0] + fm + "---\n" + "".join(lines[i + 1:])
                return rebuilt
            return None
    return None


# A corrupted closing delimiter that was MEANT to be `---` (table/pipe/
# line-number noise) — consumed (dropped) rather than kept in the body.
_CORRUPT_CLOSE_RE = re.compile(r"^[ \t]*(\d+\|)?\|?-{2,}(\|-*)*\|?[ \t]*$")
# Unambiguous body-start markers (none are valid top-level YAML keys) for a
# frontmatter that lost its proper closing `---`. Generic prose is handled
# via the "not YAML-shaped" path instead, to avoid colliding with `key:`.
_BODY_MARKER_RE = re.compile(
    r"^\*\*"                # **bold** (URL/Source/Author/Publisher metadata)
    r"|^#{1,6}\s"           # markdown heading
    r"|^\|"                 # table row / pipe-corrupted delimiter
    r"|^-[ \t]"             # col-0 list bullet (body summary bullets)
)


def repair_missing_close(text: str) -> str | None:
    """Class E. Frontmatter that lost its closing `---` so the body bled in.
    Find the first body-marker line; if the preceding lines parse with type:,
    insert `---` there (consuming a corrupted-close marker if that's what the
    body line is). Body from the split point on is preserved verbatim."""
    mo = _FM_OPEN_RE.match(text)
    if not mo:
        return None
    # not our case if there's already a clean parseable close
    m = re.match(r"\A---[ \t]*\n(.*?)\n---", text, re.S)
    if m:
        try:
            if isinstance(yaml.safe_load(m.group(1)), dict):
                return None
        except Exception:
            pass
    after = text[mo.end():]
    lines = after.splitlines(keepends=True)
    body_idx = None
    for i, ln in enumerate(lines):
        s = ln.rstrip("\n")
        is_yaml = bool(
            _YAML_KEY_RE.match(s) or _YAML_LIST_RE.match(s)
            or _BLANK_RE.match(s) or _YAML_CONT_RE.match(s)
        )
        is_strong_body = bool(_BODY_MARKER_RE.match(s))
        if is_strong_body:
            body_idx = i      # **/#/|/col-0-bullet → unambiguous body
            break
        if is_yaml:
            continue          # still in frontmatter
        body_idx = i          # non-YAML-shaped line (prose) → body starts
        break
    if body_idx is None:
        return None
    # walk back over trailing blank lines (keep them out of frontmatter tail)
    end = body_idx
    while end > 0 and _BLANK_RE.match(lines[end - 1].rstrip("\n")):
        end -= 1
    fm_text = "".join(lines[:end])
    if not parses_with_type(fm_text):
        return None
    # consume a corrupted-close marker if that's the body-start line
    body_start = body_idx
    if _CORRUPT_CLOSE_RE.match(lines[body_idx].rstrip("\n")):
        body_start = body_idx + 1
    body = "".join(lines[body_start:])
    fm_block = fm_text if fm_text.endswith("\n") else fm_text + "\n"
    sep = "" if body.startswith("\n") else "\n"
    return "---\n" + fm_block + "---\n" + sep + body


_WIKILINK_TOKEN = re.compile(r"\[\[[^\]\n]+\]\]")
# a frontmatter line `key: [[a]], [[b]]` — value STARTS with `[[` (bare,
# unquoted) which opens a YAML nested flow sequence and fails to parse.
_WIKILINK_FLOW_LINE = re.compile(r"^([A-Za-z_][\w-]*):[ \t]*(\[\[[^\n]*)$", re.MULTILINE)


def repair_wikilink_flow(text: str) -> str | None:
    """Class F. A frontmatter field written as an unquoted inline flow of
    wikilinks — `entities: [[entities/x]], [[entities/y]]` — which is invalid
    YAML (the leading `[[` opens a nested flow sequence). Convert each such
    line to a quoted block list. Only if it then parses with type:."""
    m = re.match(r"\A(---[ \t]*\n)(.*?\n)(---[ \t]*(?:\n|\Z))", text, re.S)
    if not m:
        return None
    fm = m.group(2)
    if not _WIKILINK_FLOW_LINE.search(fm):
        return None

    def _block(mo: re.Match) -> str:
        key, val = mo.group(1), mo.group(2)
        toks = _WIKILINK_TOKEN.findall(val)
        if not toks:
            return mo.group(0)
        return key + ":\n" + "\n".join(f'  - "{t}"' for t in toks)

    new_fm = _WIKILINK_FLOW_LINE.sub(_block, fm)
    if new_fm == fm or not parses_with_type(new_fm):
        return None
    return m.group(1) + new_fm + m.group(3) + text[m.end():]


def repair_catn_prefix(text: str) -> str | None:
    """Class G. An agent wrote ``cat -n`` / Read-tool output back into the file,
    prefixing lines with ``     12|`` and/or mangling the opening fence to
    ``|---``. Strip those prefixes. STRICTLY GATED: return the result only if it
    THEN yields a parseable frontmatter block with ``type:``. Jumbled cases
    (spurious extra ``---`` fences, broken indentation) fail the gate and are
    left for manual/agent repair rather than written half-fixed.

    Edge case handled: a ``|``/number prefix on the opening fence can break
    ``_FM_OPEN_RE`` so the page is wrongly treated as "no frontmatter, skip" and
    is silently un-repairable; this path catches that.
    """
    lines = text.split("\n")
    if not lines:
        return None
    catn = any(_CATN_PREFIX_RE.match(ln) for ln in lines)
    pipe = _PIPE_FENCE_RE.match(lines[0]) or lines[0].startswith("|")
    if not (catn or pipe):
        return None

    # 1. cat -n number+pipe prefixes are unambiguous (a markdown table row starts
    #    with "|", never "<digits>|") — strip from EVERY line, body included.
    if catn:
        lines = [_CATN_PREFIX_RE.sub("", ln) for ln in lines]

    # 2. Leading "|" pipes (the gateway Read display separator, echoed back by a
    #    weak model) are ambiguous with markdown tables — so strip them ONLY in
    #    the frontmatter region: line 0 through the de-piped closing fence. Body
    #    tables are left untouched. Run unconditionally: it's a no-op on a clean
    #    FM, and a combined cat-n+pipe file (e.g. "   20||---" close) leaves a
    #    pipe on the close even after step 1 stripped the number.
    out: list[str] = []
    closed = False
    for i, ln in enumerate(lines):
        if closed:
            out.append(ln)
            continue
        d = re.sub(r"^\|+", "", ln)
        out.append(d)
        if i == 0:
            if d.strip() != "---":
                return None  # no clean opening fence — bail
        elif d.strip() == "---":
            closed = True
    if not closed:
        return None
    lines = out

    new = "\n".join(lines)
    if new == text:
        return None
    m = re.match(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", new, re.S)
    if not m or not parses_with_type(m.group(1)):
        return None
    # Guard against a lossy early-close on jumbled files (e.g. a spurious mid-FM
    # fence): a stray "---" inside the recovered block, OR a body that opens with
    # an orphaned YAML list item / key, means we closed early and dropped
    # content — bail and leave it for manual repair rather than write a lossy fix.
    if re.search(r"\n---[ \t]*\n", m.group(1)):
        return None
    first_body = next((b for b in new[m.end():].split("\n") if b.strip()), "")
    first_body = re.sub(r"^\|+", "", first_body)  # an orphan may still be piped
    if re.match(r"^\s*-[ \t]|^[A-Za-z_][\w-]*:[ \t]", first_body):
        return None
    return new


def repair_text(text: str) -> tuple[str | None, str]:
    """Try each repair in order; return (new_text|None, class_label)."""
    r = repair_catn_prefix(text)
    if r is not None:
        return r, "catn-prefix"
    r = repair_body_bleed(text)
    if r is not None:
        return r, "body-bleed"
    r = repair_missing_close(text)
    if r is not None:
        return r, "missing-close"
    r = repair_wikilink_flow(text)
    if r is not None:
        return r, "wikilink-flow"
    r = repair_missing_close(text)
    if r is not None:
        return r, "missing-close"
    r = repair_inline_flow(text)
    if r is not None:
        return r, "inline-flow"
    r = repair_trailing_garbage(text)
    if r is not None:
        return r, "trailing-garbage"
    r = repair_malformed_close(text)
    if r is not None:
        return r, "malformed-close"
    return None, ""


def _already_valid(text: str) -> bool:
    """True = nothing to fix. A file with NO opening `---` has no frontmatter
    (skip). A file WITH an opening `---` must have a closing `---` whose block
    parses; otherwise it's broken (body-bleed or syntax) and needs repair."""
    head = text.split("\n", 60)
    # cat -n / pipe-fence corruption looks frontmatter-ish but breaks _FM_OPEN_RE;
    # do NOT treat it as "no frontmatter" — it needs repair (repair_catn_prefix).
    if (head and _PIPE_FENCE_RE.match(head[0])) or any(_CATN_PREFIX_RE.match(ln) for ln in head[:60]):
        return False
    if not _FM_OPEN_RE.match(text):
        return True  # no frontmatter at all
    m = re.match(r"\A---[ \t]*\n(.*?)\n---", text, re.S)
    if not m:
        return False  # opening but no close → body-bleed, needs repair
    try:
        yaml.safe_load(m.group(1))
        return True
    except Exception:
        return False


def main() -> int:
    if not WIKI.is_dir():
        print(f"ERROR: {WIKI} does not exist", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 1

    fixed = {"catn-prefix": 0, "body-bleed": 0, "missing-close": 0, "wikilink-flow": 0, "inline-flow": 0, "trailing-garbage": 0, "malformed-close": 0}
    still_broken = 0
    perm_skips = 0
    print("=== repair-broken-frontmatter ===")
    for path in sorted(WIKI.rglob("*.md")):
        if _is_skippable(path):
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if _already_valid(text):
            continue
        new_text, label = repair_text(text)
        if new_text is None:
            still_broken += 1
            print(f"  ? {path.relative_to(VAULT)}: still broken (left quarantined)")
            continue
        try:
            path.write_text(new_text)
        except PermissionError:
            perm_skips += 1
            print(f"  ! {path.relative_to(VAULT)}: PERMISSION DENIED — chmod 646 + re-run")
            continue
        fixed[label] += 1
        print(f"  + {path.relative_to(VAULT)}: repaired ({label})")

    print()
    print(f"Repaired: {fixed['catn-prefix']} catn-prefix, {fixed['body-bleed']} body-bleed, {fixed['missing-close']} missing-close, "
          f"{fixed['wikilink-flow']} wikilink-flow, {fixed['inline-flow']} inline-flow, "
          f"{fixed['trailing-garbage']} trailing-garbage, {fixed['malformed-close']} malformed-close.")
    print(f"Still broken (need manual/agent repair): {still_broken}.")
    if perm_skips:
        print(f"Permission-skipped: {perm_skips}.")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
