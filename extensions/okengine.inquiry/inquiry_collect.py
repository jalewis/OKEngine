#!/usr/bin/env python3
"""inquiry_collect.py — okengine.inquiry: run each open inquiry's terms through its declared
source connector. no_agent, deterministic, bounded, idempotent. Zero model budget.

This lane is the reason the `terms` field exists. Collection is driven by the inquiry PAGE, so
the statement of what a vault researches and the act of researching it cannot drift apart. There
is no second copy of the terms in a cron definition, a prompt, or a service's database.

It opens no socket of its own: every fetch goes through the engine's declarative
`source_connector.py` runtime, which owns the allowed-hosts / private-network / rate-limit /
archive contract and holds the secret references. This lane only decides WHICH connector runs
with WHICH inputs, and records what came back.

Parameter binding
-----------------
A term's `query` is bound to the connector's single required input. A connector with several
required inputs must have the rest supplied by the inquiry's `collection_params` (applied to
every term) or the term's own `params` (which win). A connector with NO required inputs cannot
be parameterized by a term at all and is a contract error, not a silent full-firehose pull.

Precedence, narrowest last: connector defaults < inquiry.collection_params < term.params.

TRAP — do not inherit another domain's relevance floor
------------------------------------------------------
Collection services score records for their OWN primary domain, and those scores are noise or
worse outside it. Measured on one deployment's news service (okengine#746): the same story about
AI extinction risk scored 0.008 from a tier-2 technology outlet and 0.886 from a state
broadcaster. A `min_security_relevance >= 0.5` floor — correct for that service's security
vault — would have kept the propaganda copy and dropped the reputable one. So this lane applies
NO score floor of its own and passes through only what the inquiry explicitly declares. An
inquiry outside a connector's home domain should leave such knobs unset.

Env: WIKI_PATH (/opt/vault) · OKENGINE_INQUIRY_CONNECTOR_DIR ·
     OKENGINE_INQUIRY_MAX_INQUIRIES (20) · OKENGINE_INQUIRY_MAX_TERMS_PER_INQUIRY (12)
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inquiry_lib as lib  # noqa: E402

ENGINE_ROOT = Path(__file__).resolve().parents[2]
CONNECTOR_RUNTIME = ENGINE_ROOT / "scripts/cron/source_connector.py"
STAGED_RUNTIME = Path("/opt/data/scripts/source_connector.py")
STATE = lib.VAULT / ".okengine/inquiry/collect-state.json"
MAX_INQUIRIES = int(os.environ.get("OKENGINE_INQUIRY_MAX_INQUIRIES", "20"))
MAX_TERMS = int(os.environ.get("OKENGINE_INQUIRY_MAX_TERMS_PER_INQUIRY", "12"))


def connector_dir() -> Path:
    """Where deployed manifests live. deploy-cron-scripts.sh stages a pack's connectors/ into
    /opt/data/config/connectors/; a checkout or test points OKENGINE_INQUIRY_CONNECTOR_DIR."""
    override = os.environ.get("OKENGINE_INQUIRY_CONNECTOR_DIR")
    if override:
        return Path(override)
    deployed = Path("/opt/data/config/connectors")
    if deployed.is_dir():
        return deployed
    return lib.VAULT / "connectors"


def _runtime_module():
    path = CONNECTOR_RUNTIME if CONNECTOR_RUNTIME.is_file() else STAGED_RUNTIME
    spec = importlib.util.spec_from_file_location("okengine_source_connector", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, path


def discover_connectors(directory: Path) -> tuple[dict[str, Path], dict[str, dict], list[str]]:
    """Map connector id -> (manifest path, manifest). Malformed manifests are reported, not
    skipped silently: a connector that fails to parse is indistinguishable at the dossier from
    one that simply found nothing."""
    paths: dict[str, Path] = {}
    manifests: dict[str, dict] = {}
    problems: list[str] = []
    if not directory.is_dir():
        return paths, manifests, [f"connector directory not found: {directory}"]
    module, _ = _runtime_module()
    # glob-ok: a flat configuration directory, never a content namespace.
    for path in sorted((*directory.glob("*.yaml"), *directory.glob("*.yml"))):
        try:
            manifest = module.load_yaml(path)
            errors = module.validate_manifest(manifest)
        except Exception as exc:  # noqa: BLE001 — report every malformed manifest, don't abort
            problems.append(f"{path.name}: {exc}")
            continue
        if errors:
            problems.extend(f"{path.name}: {e}" for e in errors)
            continue
        cid = str(manifest.get("id") or "")
        if cid in paths:
            problems.append(f"{path.name}: duplicate connector id {cid!r} "
                            f"(already declared by {paths[cid].name})")
            continue
        paths[cid] = path
        manifests[cid] = manifest
    return paths, manifests, problems


def query_input(manifest: dict) -> tuple[str, str | None]:
    """Return (input_name, error). The term's query binds to the connector's single required
    input; more than one required input means the inquiry must name the rest itself."""
    required = list((manifest.get("inputs") or {}).get("required") or [])
    if not required:
        return "", (f"connector {manifest.get('id')!r} declares no required inputs, so a term "
                    "cannot parameterize it — an inquiry must not trigger an unfiltered pull")
    return str(required[0]), None


def build_params(inquiry: lib.Inquiry, term: lib.Term, input_name: str) -> dict[str, str]:
    """Narrowest wins: inquiry.collection_params < term.params, with the query bound last so a
    term can never accidentally overwrite its own query through a stray param."""
    params: dict[str, str] = dict(inquiry.collection_params)
    params.update(term.params)
    params[input_name] = term.query
    return params


def run_connector(manifest_path: Path, params: dict[str, str], timeout: int = 180) -> dict:
    """Invoke the connector runtime in a child process. Isolated on purpose: the runtime does
    network I/O and holds secrets, and one term's failure must not abort the run. Replaced
    wholesale in tests."""
    runtime = CONNECTOR_RUNTIME if CONNECTOR_RUNTIME.is_file() else STAGED_RUNTIME
    argv = [sys.executable, str(runtime), "--manifest", str(manifest_path)]
    for name, value in sorted(params.items()):
        argv += ["--param", f"{name}={value}"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "records": 0, "error": f"timeout after {timeout}s"}
    if proc.returncode != 0:
        return {"ok": False, "records": 0,
                "error": (proc.stderr or proc.stdout or "").strip()[:300] or
                         f"exit {proc.returncode}"}
    records = 0
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and "records" in payload:
            records = int(payload.get("records") or 0)
    return {"ok": True, "records": records, "error": ""}


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    check_only = "--validate" in argv

    inquiries, contract_errors = lib.load_inquiries()
    directory = connector_dir()
    paths, manifests, problems = discover_connectors(directory)
    reach_errors = lib.connector_errors(inquiries, set(paths))

    for problem in problems:
        print(f"WARN  connector {problem}")
    for error in contract_errors + reach_errors:
        print(f"FAIL  {error}")
    if contract_errors or reach_errors:
        # Loud and non-zero. A malformed inquiry that merely warns produces an empty dossier,
        # which an operator reads as "this field is quiet" rather than "my page is broken".
        print(json.dumps({"wakeAgent": False, "inquiries": len(inquiries),
                          "errors": len(contract_errors) + len(reach_errors)}))
        return 1
    if check_only:
        print(f"OK — {len(inquiries)} inquiry page(s) valid against "
              f"{len(paths)} connector(s) in {directory}")
        return 0
    if not inquiries:
        print(f"# no inquiry pages under {lib.WIKI / lib.NS} — nothing to collect. "
              "An inquiry DECLARES a research topic; this lane never invents one.")
        print(json.dumps({"wakeAgent": False, "inquiries": 0}))
        return 0

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    state = _load_state()
    runs = state.setdefault("terms", {})

    active = sorted((i for i in inquiries if i.is_open), key=lambda i: (i.opened or "9999", i.slug))
    deferred = max(0, len(active) - MAX_INQUIRIES)
    total_records = total_terms = failures = 0

    for inquiry in active[:MAX_INQUIRIES]:
        manifest = manifests[inquiry.connector]
        input_name, input_error = query_input(manifest)
        if input_error:
            print(f"FAIL  {lib.NS}/{inquiry.slug}: {input_error}")
            failures += 1
            continue
        for term in inquiry.terms[:MAX_TERMS]:
            params = build_params(inquiry, term, input_name)
            outcome = run_connector(paths[inquiry.connector], params)
            total_terms += 1
            key = f"{inquiry.slug}::{term.key}"
            prior = runs.get(key) or {}
            record = {"last_run": today, "connector": inquiry.connector, "query": term.query,
                      "records": outcome["records"], "ok": outcome["ok"],
                      "error": outcome["error"],
                      "last_yield": today if outcome["records"] else prior.get("last_yield", ""),
                      "total_records": int(prior.get("total_records") or 0) + outcome["records"]}
            runs[key] = record
            if outcome["ok"]:
                total_records += outcome["records"]
                print(f"  {inquiry.slug} :: {term.key} -> {outcome['records']} record(s)")
            else:
                failures += 1
                print(f"  {inquiry.slug} :: {term.key} -> FAILED: {outcome['error']}")
        if len(inquiry.terms) > MAX_TERMS:
            print(f"  {inquiry.slug}: {len(inquiry.terms) - MAX_TERMS} term(s) deferred "
                  f"(OKENGINE_INQUIRY_MAX_TERMS_PER_INQUIRY={MAX_TERMS})")

    state["last_run"] = now.isoformat()
    _save_state(state)
    if deferred:
        print(f"# {deferred} open inquiry(s) deferred to the next run "
              f"(OKENGINE_INQUIRY_MAX_INQUIRIES={MAX_INQUIRIES})")
    print(f"# {len(active)} open inquiry(s), {total_terms} term(s) run, "
          f"{total_records} record(s), {failures} failure(s)")
    print(json.dumps({"wakeAgent": False, "inquiries": len(active), "terms": total_terms,
                      "records": total_records, "failures": failures}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
