#!/usr/bin/env python3
"""adjudication_eval — measure whether a given model can be the janitor (okengine#551).

#551 proposes letting an agent DECIDE claim adjudication instead of parking it in a human queue
nobody works. That is only safe if the deciding model can actually read, so this harness answers
"which model" with numbers instead of priors.

It is deliberately domain-agnostic: every case, claim and expected answer comes from a gold-set
file supplied with --cases. No CTI vocabulary lives here, so the same harness grades a
vendor-risk or competitive-intel pack.

## What it measures

Three scores, in descending order of how much they should influence the decision:

  quote-exists   Does the warrant the model returned appear VERBATIM in the source page it named?
                 Mechanical, no grading, no judgment. A model that invents supporting quotes fails
                 here regardless of whether its verdict was lucky. This is the single most
                 important number: a fabricated warrant is worse than a wrong verdict, because it
                 is designed to survive review.

  accuracy       Verdict vs the gold label.

  bias-delta     The gold set is PAIRED: each real claim is accompanied by a mutated twin whose
                 number/date has been altered so it now contradicts its source. A model that says
                 "supported" to both is not reading, it is agreeing. bias-delta = accuracy on TRUE
                 claims minus accuracy on MUTATED claims. Near zero is good; large positive means
                 the model rubber-stamps. This exists because CHE measured a 12:1 confirmation bias
                 in this codebase's own judgment lanes — a sycophantic model would silently mark a
                 whole backlog "fine".

## Prompting

Refutation-shaped, per #551 constraint 3: the model is asked to REFUTE the claim and to return
`supported` only when refutation fails. Confirm-shaped prompts confirm.

All model traffic goes through `scripts/cron/llm_lib.py`, the one sanctioned direct-call path —
enforced by tests/test_llm_call_discipline.py. That is not bureaucracy here: the first draft of this
harness sent `think: false` + `chat_template_kwargs`, which llm_lib's docstring records as NOT
honored by Ollama-style /v1 endpoints. The knob that works is `reasoning_effort: "none"`, which
llm_lib bakes in. A harness that measured models while silently leaving reasoning ON would have
graded thinking-token budget instead of reading ability.

Env: DEEPSEEK_API_KEY for the deepseek provider. Local models need no key.
Exit code is 0 whenever the run completed — a poor score is a result, not an error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cron"))
import llm_lib  # noqa: E402  — the ONE sanctioned model-call path (test_llm_call_discipline)

PROMPT = """\
You are auditing a knowledge-base page against its cited evidence. Your job is to REFUTE the claim.

Return `supported` ONLY if you tried to refute it and failed. If the evidence does not settle the
question, that is `unsupported` — absence of evidence is not support. If the evidence states
something that conflicts with the claim, that is `contradicted`.

CLAIM UNDER AUDIT:
{claim}

EVIDENCE (the only evidence you may use — do not use outside knowledge):
{evidence}

Reply with ONE JSON object and nothing else:
{{"verdict": "supported" | "unsupported" | "contradicted",
  "source_id": "<the SOURCE id above that settles it, or null>",
  "warrant": "<a VERBATIM span copied from that source that settles it, or null>",
  "reasoning": "<one sentence>"}}

The warrant must be copied character-for-character from the evidence. Do not paraphrase it.
Do not invent a quote. If you have no verbatim span, use null.
"""

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "")).strip().lower()


def _read_page(vault: Path, rel: str) -> str:
    p = vault / "wiki" / (rel if rel.endswith(".md") else rel + ".md")
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# llm_lib omits the key entirely for None; "none" disables reasoning. Both are meaningful and
# they are NOT the same request, so the sweep can name each.
_OMIT = "omit"


def _effort(level: str) -> str | None:
    return None if level == _OMIT else level


def _call(base_url: str, model: str, prompt: str, api_key: str | None, timeout: int,
          retries: int = 3, reasoning_effort: str | None = "none") -> tuple[str, float]:
    """One graded call, timed, through llm_lib.

    llm_lib owns the reasoning-off policy and its own transient-transport backoff, so this adds
    only the stopwatch. A failure that survives its retries surfaces as LLMError and is scored as
    NO RESULT rather than as a model failure — a busy provider is not a capability finding.
    """
    t0 = time.monotonic()
    out = llm_lib.chat(prompt, model=model, base_url=base_url, api_key=api_key,
                       max_tokens=900, temperature=0.0, timeout=timeout, retries=retries,
                       reasoning_effort=reasoning_effort)
    return out, time.monotonic() - t0


def _parse(text: str) -> dict | None:
    """Pull the JSON object out of a reply that may be fenced or prefaced with prose."""
    if not text:
        return None
    t = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.M)
    # Strip a leaked <think> block rather than failing the model for the shim not catching it.
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S)
    start = t.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(t)):
            if t[i] == "{":
                depth += 1
            elif t[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(t[start:i + 1])
                        return obj if isinstance(obj, dict) else None
                    except json.JSONDecodeError:
                        break
        start = t.find("{", start + 1)
    return None


def run_case(case: dict, base_url: str, model: str, api_key: str | None, timeout: int,
             reasoning_effort: str | None = "none") -> dict:
    vault = Path(case["vault"])
    blocks, texts = [], {}
    for s in case["evidence"]:
        body = _read_page(vault, s)
        texts[s] = body
        blocks.append(f"--- SOURCE id={s} ---\n{body.strip()}\n")
    evidence = "\n".join(blocks) if blocks else "(no evidence pages are cited by this page)"
    prompt = PROMPT.format(claim=case["claim"].strip(), evidence=evidence)

    try:
        raw, secs = _call(base_url, model, prompt, api_key, timeout,
                          reasoning_effort=reasoning_effort)
    except (llm_lib.LLMError, OSError, KeyError, ValueError, TimeoutError) as exc:
        # transport=True marks this as "no result", distinct from a model that replied badly.
        return {"id": case["id"], "error": f"{type(exc).__name__}: {exc}", "verdict": None,
                "correct": False, "quote_ok": None, "secs": None, "parsed": False,
                "transport": True}

    obj = _parse(raw)
    if obj is None:
        return {"id": case["id"], "error": "unparseable reply", "verdict": None, "correct": False,
                "quote_ok": None, "secs": round(secs, 1), "parsed": False,
                "raw_head": raw[:160].replace("\n", " ")}

    verdict = str(obj.get("verdict") or "").strip().lower()
    warrant = obj.get("warrant")
    sid = obj.get("source_id")
    # quote-exists: only meaningful when the model claimed a warrant. A null warrant on an
    # `unsupported`/`contradicted` verdict is honest, not a failure — score it None, not False.
    quote_ok = None
    if isinstance(warrant, str) and warrant.strip():
        hay = _norm(texts.get(str(sid), "")) or " ".join(_norm(v) for v in texts.values())
        quote_ok = _norm(warrant) in hay
    return {"id": case["id"], "verdict": verdict, "gold": case["gold"],
            "correct": verdict == case["gold"], "quote_ok": quote_ok, "secs": round(secs, 1),
            "parsed": True, "mutated": bool(case.get("mutated")), "error": None,
            "warrant": (warrant or "")[:120] if isinstance(warrant, str) else None}


def score(results: list[dict]) -> dict:
    # A transport failure is not a model result: exclude it from the denominator entirely rather
    # than scoring a busy provider as an incapable model.
    graded = [r for r in results if not r.get("transport")]
    done = [r for r in graded if r["parsed"]]
    claimed = [r for r in done if r["quote_ok"] is not None]
    true_c = [r for r in done if not r.get("mutated")]
    mut_c = [r for r in done if r.get("mutated")]

    def acc(rs):
        return (sum(r["correct"] for r in rs) / len(rs)) if rs else None

    return {
        "cases": len(results),
        "transport_failures": sum(1 for r in results if r.get("transport")),
        "graded": len(graded),
        "parsed": len(done),
        "parse_rate": round(len(done) / len(graded), 3) if graded else None,
        "accuracy": round(acc(done), 3) if done else None,
        "quote_exists": round(sum(r["quote_ok"] for r in claimed) / len(claimed), 3) if claimed else None,
        "warrants_claimed": len(claimed),
        "acc_true": round(acc(true_c), 3) if true_c else None,
        "acc_mutated": round(acc(mut_c), 3) if mut_c else None,
        "bias_delta": (round(acc(true_c) - acc(mut_c), 3) if true_c and mut_c else None),
        "median_secs": (sorted(r["secs"] for r in done)[len(done) // 2] if done else None),
        "model_failures": sum(1 for r in graded if not r["parsed"]),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True, help="gold-set YAML (kept OUT of this repo — it is domain data)")
    ap.add_argument("--model", required=True)
    ap.add_argument("--base-url", required=True, help="OpenAI-compatible /v1 base url")
    ap.add_argument("--api-key-env", default="", help="env var holding the key (cloud providers)")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--out", help="write per-case results as JSON")
    ap.add_argument("--limit", type=int, help="run only the first N cases")
    ap.add_argument("--reasoning-effort", default="none",
                    help="comma-separated levels to sweep, e.g. 'none,high'. "
                         f"'{_OMIT}' sends no reasoning key at all. llm_lib defaults every caller "
                         "to 'none', which is right for bulk classification and measurably wrong "
                         "for adjudication — so this is swept, never assumed.")
    args = ap.parse_args(argv)

    cases = yaml.safe_load(Path(args.cases).read_text(encoding="utf-8"))["cases"]
    if args.limit:
        cases = cases[:args.limit]
    key = os.environ.get(args.api_key_env) if args.api_key_env else None
    if args.api_key_env and not key:
        print(f"ERROR: {args.api_key_env} is empty — refusing to run and report a vacuous zero",
              file=sys.stderr)
        return 1

    levels = [x.strip() for x in args.reasoning_effort.split(",") if x.strip()]
    sweep = {}
    for level in levels:
        print(f"\n--- reasoning_effort={level} ---")
        results = []
        for i, c in enumerate(cases, 1):
            r = run_case(c, args.base_url, args.model, key, args.timeout,
                         reasoning_effort=_effort(level))
            results.append(r)
            flag = "ok " if r["correct"] else "MISS"
            q = {True: "q+", False: "q!", None: "q0"}[r["quote_ok"]]
            print(f"  [{i:2d}/{len(cases)}] {flag} {q} {r['id']:<34} "
                  f"verdict={r['verdict']} gold={c['gold']} {r.get('error') or ''}")
        sweep[level] = {"score": score(results), "results": results}

    print(f"\n=== {args.model} ===")
    cols = ["accuracy", "quote_exists", "bias_delta", "parse_rate", "median_secs",
            "transport_failures", "model_failures"]
    print(f"  {'reasoning_effort':<18} " + " ".join(f"{c:>18}" for c in cols))
    for level in levels:
        sc = sweep[level]["score"]
        print(f"  {level:<18} " + " ".join(f"{str(sc[c]):>18}" for c in cols))
    if args.out:
        Path(args.out).write_text(json.dumps({"model": args.model, "sweep": sweep}, indent=2),
                                  encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
