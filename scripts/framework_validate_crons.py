"""Cron-definition validation service used by framework validate."""
from __future__ import annotations

import ast
import importlib
import json
import re
from pathlib import Path
from typing import Any, Callable


def prompt_text(pack: Path, value: Any) -> str:
    """Resolve an inline prompt or a pack-relative prompt_file reference."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    inline = value.get("prompt")
    if isinstance(inline, str) and inline.strip():
        return inline
    reference = value.get("prompt_file")
    if not isinstance(reference, str) or not reference.strip():
        return ""
    root = pack.resolve()
    target = (pack / reference).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"prompt_file escapes pack root: {reference}")
    if not target.is_file():
        raise ValueError(f"prompt_file not found: {reference}")
    return target.read_text(encoding="utf-8")


def check_crons(
    pack: Path,
    report: Any,
    *,
    validator_dir: Path,
    engine_cron_dir: Path,
    load_yaml: Callable[[Path], Any],
    cron_expr: Callable[[dict], str],
    dst_schedule_problem: Callable[[str], str | None],
) -> None:
    r = report
    oc = None
    ac = None
    try:
        import importlib.util
        oc_path = validator_dir / "cron" / "output_contract.py"
        spec = importlib.util.spec_from_file_location("okengine_output_contract", oc_path)
        oc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(oc)
        ac_path = validator_dir / "cron" / "artifact_contract.py"
        ac_spec = importlib.util.spec_from_file_location("okengine_artifact_contract", ac_path)
        ac = importlib.util.module_from_spec(ac_spec)
        ac_spec.loader.exec_module(ac)
    except Exception as exc:
        r.fail("cron output contracts", f"validator failed: {exc}")
    cdir = pack / "crons"
    if not cdir.is_dir():
        r.warn("crons/", "absent — pack contributes no cron defs")
        return
    dc = cdir / "domain-crons.json"
    defs = []
    if dc.is_file():
        try:
            defs = json.loads(dc.read_text(encoding="utf-8"))
            if not isinstance(defs, list):
                r.fail("crons/domain-crons.json shape", "must be a JSON array")
                defs = []
            else:
                r.ok("crons/domain-crons.json", f"{len(defs)} domain cron(s)")
        except Exception as e:
            r.fail("crons/domain-crons.json parses", f"JSON error: {str(e)[:120]}")
    else:
        r.info("crons/domain-crons.json", "absent (no domain crons)")
    etp = cdir / "engine-template-prompts.json"
    if etp.is_file():
        try:
            prompts = json.loads(etp.read_text(encoding="utf-8"))
            if not isinstance(prompts, dict):
                r.fail("crons/engine-template-prompts.json shape", "must be a JSON object")
            else:
                # An engine-template lane pairs an engine wake-gate script with a
                # pack-supplied prompt; an empty prompt = the agent wakes with no
                # instructions, so the lane is broken.
                empties = [
                    key for key, value in prompts.items()
                    if not prompt_text(pack, value).strip()
                ]
                (r.fail if empties else r.ok)(
                    "crons/engine-template-prompts.json",
                    f"empty prompt(s): {empties}" if empties else f"{len(prompts)} prompt(s)")
                try:
                    contract_errors = []
                    for name, value in prompts.items():
                        if isinstance(value, dict):
                            unknown = sorted(
                                set(value) - {"prompt", "prompt_file", "output_contract"}
                            )
                            if unknown:
                                contract_errors.append(
                                    f"engine-template prompt {name!r} has unknown key(s): {unknown}")
                            if "output_contract" in value:
                                contract_errors.extend(oc.validate(
                                    value["output_contract"],
                                    f"engine-template prompt {name!r} output_contract"))
                    for error in contract_errors:
                        r.fail("cron output contract", error)
                    if not contract_errors:
                        r.ok("cron output contracts", "engine-template contract shapes valid")
                except Exception as exc:
                    r.fail("cron output contracts", f"validator failed: {exc}")
        except Exception as e:
            r.fail("crons/engine-template-prompts.json parses", f"JSON error: {str(e)[:120]}")
    # shape + script existence for domain defs
    sdir = cdir / "scripts"
    engine_sdir = engine_cron_dir
    for d in defs:
        if not isinstance(d, dict) or not d.get("name"):
            r.fail("domain cron shape", f"entry missing name: {str(d)[:80]}")
            continue
        # A cron with no usable schedule expression can't be scheduled; one with
        # neither a script nor a prompt has nothing to run — both break the deploy.
        problems = []
        expr = cron_expr(d)
        if not expr:
            problems.append("no usable schedule expr")
        else:
            dst_problem = dst_schedule_problem(expr)
            if dst_problem:
                problems.append(dst_problem)
        if not (d.get("script") or d.get("prompt")):
            problems.append("no script or prompt")
        if problems:
            r.fail(f"cron '{d.get('name')}'", " + ".join(problems))
        if not d.get("no_agent") and d.get("enabled_toolsets") is None:
            r.fail("cron tool surface",
                   f"agent cron {d.get('name')!r} must declare enabled_toolsets explicitly; "
                   "null silently prevents MCP tools from reaching the model")
        if d.get("output_contract") is not None:
            errors = (oc.validate(d["output_contract"], f"cron {d.get('name')!r} output_contract")
                      if oc is not None else ["output-contract validator unavailable"])
            for error in errors:
                r.fail("cron output contract", error)
            contract = d.get("output_contract") or {}
            if contract.get("required_write_path"):
                iterations = d.get("max_iterations")
                if (not isinstance(iterations, int) or isinstance(iterations, bool)
                        or iterations < 6):
                    r.fail(
                        "cron iteration budget",
                        f"model-writing cron {d.get('name')!r} promises required_write_path but "
                        f"max_iterations={iterations!r}; read/synthesize/write lanes require at "
                        "least 6 bounded iterations",
                    )
            fixtures = d.get("adversarial_fixtures")
            if not isinstance(fixtures, list) or not fixtures or any(
                    not isinstance(item, str) or not item.strip() for item in fixtures):
                r.fail("cron adversarial fixtures",
                       f"model-writing cron {d.get('name')!r} with a contract must declare "
                       "adversarial_fixtures")
        elif not d.get("no_agent") and any(
                str(tool) == "okengine-write" or str(tool).startswith("okengine-write-")
                for tool in (d.get("enabled_toolsets") or [])) \
                and not d.get("output_contract_exempt"):
            r.fail("cron output contract",
                   f"model-writing cron {d.get('name')!r} must declare output_contract")
        if d.get("artifact_contract") is not None:
            if d.get("no_agent") is not True:
                r.fail("cron artifact contract",
                       f"cron {d.get('name')!r} artifact_contract is only valid for no_agent jobs")
            errors = (ac.validate(
                d["artifact_contract"], f"cron {d.get('name')!r} artifact_contract")
                if ac is not None else ["artifact-contract validator unavailable"])
            for error in errors:
                r.fail("cron artifact contract", error)
        scr = d.get("script") or ""
        if scr:
            base = Path(scr).name
            if (engine_sdir / base).is_file() and not (sdir / base).is_file():
                r.info(f"cron '{d.get('name')}' script", f"{base} supplied by the engine")
            elif (engine_sdir / base).is_file():
                # BOTH have it. Saying "supplied by the engine" here is precisely how a pack fork
                # stayed invisible for three weeks: the deploy stages the PACK copy LAST, so the
                # engine's version is the one that never runs. The shadow check below owns the
                # verdict; this line must not describe the wrong file as the live one.
                r.info(f"cron '{d.get('name')}' script",
                       f"{base} is in BOTH the engine and crons/scripts/ — the PACK copy runs")
            elif not (sdir / base).is_file():
                r.warn(f"cron '{d.get('name')}' script", f"{base} not in crons/scripts/ (engine-provided?)")
        # okengine#478: a `per-selected-item` contract makes the runner verify the model's
        # receipt AGAINST the selection manifest. If the lane's selector never writes one,
        # `load_selection` raises "selection manifest unavailable" and EVERY run of that lane
        # fails verification — permanently, and for a reason that looks like a model fault.
        # Measured live: 6 engine lanes in this state, 171 failed receipts, still accruing.
        # Two surfaces that must agree with nothing enforcing the agreement, so enforce it here.
        if (d.get("output_contract") or {}).get("completion") == "per-selected-item":
            base = Path(scr).name if scr else ""
            src = None
            for cand in ((engine_sdir / base), (sdir / base)):
                if base and cand.is_file():
                    src = cand
                    break
            if not base:
                r.fail("cron selection manifest",
                       f"cron {d.get('name')!r} declares completion=per-selected-item but has "
                       "no selector script to write the manifest its receipt is verified against")
            elif src is None:
                r.warn("cron selection manifest",
                       f"cron {d.get('name')!r} selector {base!r} not found — cannot confirm it "
                       "writes the selection manifest (undetectable, not a pass)")
            elif not any(tok in src.read_text(encoding="utf-8", errors="replace")
                         for tok in ("write_selection_manifest", "OKENGINE_SELECTION_MANIFEST")):
                r.fail("cron selection manifest",
                       f"cron {d.get('name')!r} declares completion=per-selected-item but its "
                       f"selector {base!r} never writes a selection manifest — every run will "
                       "fail receipt verification with 'selection manifest unavailable'")
    # syntax-check pack scripts in-process (compile() — no .pyc side effect, so a
    # read-only/foreign-owned scripts dir never yields a false positive).
    if sdir.is_dir():
        bad = []
        for py in sorted(sdir.glob("*.py")):  # glob-ok: pack scripts/ is a flat dir, not a sharded content namespace
            try:
                compile(py.read_text(encoding="utf-8", errors="replace"), str(py), "exec")
            except SyntaxError as e:
                bad.append(f"{py.name}:{e.lineno}")
            except OSError:
                pass  # unreadable file is not a syntax verdict
        (r.fail if bad else r.ok)("crons/scripts/*.py compile",
                                  f"syntax errors in: {bad}" if bad else "all parse")

        # PACK SHADOWING AN ENGINE CRON SCRIPT.
        #
        # deploy-cron-scripts.sh stages the engine's scripts/cron/*.py FIRST and the pack's
        # crons/scripts/*.py SECOND, into the same /opt/data/scripts/. Same basename => the pack
        # copy silently wins, on every deploy, forever. Nothing else notices: the deploy exits 0,
        # the file is present, and the cron runs "successfully" doing whatever the fork does.
        #
        # Measured: okcti-test carried a pre-#267 fork of `nvd_import.py`. The engine had since
        # taken ownership of that lane ("packs select a page model instead of forking it") and the
        # regenerated cron def already passed NVD_PAGE_MODEL -- which the fork ignores. So the
        # deployment ran a three-week-stale lane whose own configuration described a different one,
        # and the only reason it surfaced was a hash comparison inside the container.
        #
        # An override is still expressible: give the pack script a DIFFERENT basename and point the
        # cron def at it. What is refused is the silent same-name collision, because that is
        # indistinguishable from a leftover.
        if engine_sdir.is_dir():
            shadows = sorted(
                py.name for py in sdir.glob("*.py")      # glob-ok: flat pack dir
                if (engine_sdir / py.name).is_file())
            (r.fail if shadows else r.ok)(
                "crons/scripts/ vs engine scripts/cron/",
                (f"pack script(s) shadow an engine cron script and win at deploy: {shadows} — "
                 "rename the pack copy (and its cron `script:`) if the override is intended, or "
                 "delete it to use the engine's") if shadows
                else "no pack script shadows an engine cron script")
        else:
            r.warn("crons/scripts/ vs engine scripts/cron/",
                   f"engine cron dir {engine_sdir} not found — shadowing is UNDETECTABLE here, "
                   "not absent")

        # PARTITION-UNAWARE WRITER (okengine#54). In a namespace the schema declares non-flat,
        # placement is okf_migrate's to decide — `canonical_key`/`write_key`/`find_page` exist so an
        # importer and the reshelve drain "can never disagree and re-open the loop". A no_agent lane
        # that hand-builds a page path bypasses all of it: it re-creates the page at its own spelling
        # every run while the drain files it under the declared strategy, and the two copies drift.
        # On one live vault that produced 48 duplicated assessment records, NINE of which disagreed
        # about whether the judgment was live or retracted — so which answer a consumer got depended
        # on which directory it walked first.
        #
        # Only a PROVABLY impossible literal fails: a fixed directory segment the declared strategy
        # could never generate (`assessments/identity/{…}.md` under by-letter, whose segments are
        # single letters). An interpolated segment might compute the right seat, so it is not judged
        # here — corpus_audit's partition_collisions catches those against the real corpus.
        # Prefer the COMPOSED schema: on a live deployment the namespace a pack writes into can be
        # partitioned by a DIFFERENT pack's declaration, so the writing pack's own schema.yaml does
        # not contain the rule that governs it. That is how this went unseen — the writer and the
        # rule live in separate repos, and nothing looked at both at once.
        _psch = None
        for _cand in (pack / ".okengine" / "composed-schema.yaml", pack / "schema.yaml"):
            if _cand.is_file():
                _psch = load_yaml(_cand)
                if isinstance(_psch, dict) and (_psch.get("partitioning") or {}).get("namespaces"):
                    break
        _pns = ((_psch or {}).get("partitioning") or {}).get("namespaces") or {}
        _partitioned = {ns: (cfg or {}).get("strategy", "flat")
                        for ns, cfg in (_pns.items() if isinstance(_pns, dict) else [])
                        if isinstance(cfg, dict) and (cfg or {}).get("strategy", "flat") != "flat"}
        offenders = []
        if _partitioned:
            # AST, not a text regex: the real offenders are f-strings whose interpolations contain
            # QUOTES (`f"assessments/identity/{rec['id']}.md"`), which no quote-delimited regex can
            # span — a text scan silently missed exactly the three lanes this check exists for.
            # Rendering each f-string to a shape with `{}` for interpolations makes them literal.
            _shape = re.compile(r"\A([a-z0-9-]+)/([A-Za-z0-9_-]+)/.*\.md\Z")

            def _literal_shape(node) -> str | None:
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    return node.value
                if isinstance(node, ast.JoinedStr):
                    out = []
                    for part in node.values:
                        if isinstance(part, ast.Constant) and isinstance(part.value, str):
                            out.append(part.value)
                        elif isinstance(part, ast.FormattedValue):
                            out.append("{}")
                        else:   # pragma: no cover - CPython's grammar admits only these two node
                            return None      # types inside JoinedStr.values; kept as a bail-out so
                            # a future AST change degrades to "no literal shape" rather than to a
                            # silently truncated one that the shape regex would then misjudge.
                    return "".join(out)
                return None

            for py in sorted(sdir.glob("*.py")):   # glob-ok: flat pack dir, not a content namespace
                try:
                    tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"), str(py))
                except (OSError, SyntaxError):
                    continue                        # syntax is the compile check's verdict, not ours
                seen: set = set()
                for node in ast.walk(tree):
                    shape = _literal_shape(node)
                    if not shape:
                        continue
                    hit = _shape.match(shape)
                    if not hit:
                        continue
                    ns, seg = hit.group(1), hit.group(2)
                    if _partitioned.get(ns) != "by-letter" or len(seg) == 1 or "{" in seg:
                        continue                   # not by-letter, a plausible letter shard, or computed
                    seen.add(f"{py.name}: {ns}/{seg}/…md")
                offenders.extend(sorted(seen))
        if offenders:
            r.fail("cron partition-aware writes",
                   f"hand-built page path(s) a by-letter namespace can never produce: "
                   f"{sorted(set(offenders))} — derive the path from okf_migrate.canonical_key() / "
                   f"write_key() so the writer and the reshelve drain cannot disagree (okengine#54)")
        elif _partitioned:
            r.ok("cron partition-aware writes",
                 f"no hand-built paths into {len(_partitioned)} partitioned namespace(s)")
