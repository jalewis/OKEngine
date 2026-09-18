#!/usr/bin/env python3
"""Exact-token recall vs context depth — the KV-precision risk, measured.

The failure mode a quantised KV cache is most likely to produce is corrupted
verbatim recall of high-entropy tokens late in a long context. That is not an
abstract worry for OKEngine: the completion-receipt contract requires the model
to reproduce 64-char sha256 digests EXACTLY, and the corpus is full of CVE ids
and version strings where one wrong character is a silent data error.

Plants labelled high-entropy facts at fixed depths in a filler context, asks for
them back verbatim, and scores exact string match by depth and context size.

Run it against each candidate serving configuration and compare — see
`docs/local-model-serving.md`. Absolute numbers are only interpretable against a
baseline from the SAME harness on the previous configuration.

Usage:
  scripts/serving_recall_eval.py --base-url http://<host>:<port>/v1 \
      --model <model> [--sizes 2000,16000,48000] [-n 4] [--label q4_0]
"""
import argparse, json, os, random, re, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "cron"))
import llm_lib       # noqa: E402  (enforced client boundary — reasoning-off default)

# Fixed corpus so runs are comparable across cache settings.
RNG = random.Random(20260726)
HEX = "0123456789abcdef"


def _sha() -> str:
    return "".join(RNG.choice(HEX) for _ in range(64))


def _cve() -> str:
    return f"CVE-{RNG.randint(2019, 2026)}-{RNG.randint(10000, 99999)}"


def _ver() -> str:
    return f"{RNG.randint(1,29)}.{RNG.randint(0,40)}.{RNG.randint(0,99)}-rc{RNG.randint(1,9)}"


KINDS = [("sha256", _sha), ("cve", _cve), ("version", _ver)]

FILLER = (
    "The advisory notes that mitigations were applied progressively across the affected "
    "estate, and that operators should review their exposure before the next maintenance "
    "window. Telemetry from the reporting period showed no further anomalous behaviour. "
)


def build_prompt(target_tokens: int, depths: list[float]) -> tuple[str, dict]:
    """Filler of ~target_tokens with one labelled fact planted at each depth."""
    approx_tok = lambda s: len(s) / 3.6
    n_filler = int(target_tokens / approx_tok(FILLER))
    lines = [FILLER for _ in range(max(n_filler, 1))]
    facts = {}
    for idx, depth in enumerate(depths):
        kind, gen = KINDS[idx % len(KINDS)]
        label = f"REF-{idx:02d}"
        value = gen()
        facts[label] = {"value": value, "kind": kind, "depth": depth}
        at = min(int(len(lines) * depth), len(lines) - 1)
        lines[at] = lines[at] + f"\nRecord {label} carries the {kind} value {value}.\n"
    return "".join(lines), facts


def ask(base_url: str, model: str, prompt: str, labels: list[str],
        timeout: int = 900) -> str:
    instruction = (
        "The document above contains records labelled REF-NN. Reproduce the value of each "
        "listed record EXACTLY as written — character for character, no truncation, no "
        "reformatting, no ellipsis.\n"
        "Reply with ONLY a JSON object mapping each label to its value.\n"
        f"Labels: {', '.join(labels)}"
    )
    # Deliberately through llm_lib: the point is to measure what the LANES experience,
    # which includes the reasoning-off policy every production call carries.
    return llm_lib.chat(prompt + "\n\n" + instruction, model=model, base_url=base_url,
                        max_tokens=2000, temperature=0.2, timeout=timeout)


def score(reply: str, facts: dict) -> dict:
    """Exact match per label. Parses the JSON object if present, else greps."""
    got = {}
    m = re.search(r"\{.*\}", reply, re.S)
    if m:
        try:
            parsed = json.loads(m.group(0))
            # A valid JSON value beginning with ``{`` is necessarily an object.
            if isinstance(parsed, dict):  # pragma: no branch
                got = {k: str(v) for k, v in parsed.items()}
        except Exception:
            pass
    results = {}
    for label, meta in facts.items():
        candidate = got.get(label)
        if candidate is None:                       # fall back to raw-text search
            candidate = meta["value"] if meta["value"] in reply else ""
        results[label] = {"ok": candidate == meta["value"], "depth": meta["depth"],
                          "kind": meta["kind"], "got": candidate[:80]}
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True, dest="base_url",
                    help="OpenAI-compatible endpoint root, e.g. http://<host>:<port>/v1")
    ap.add_argument("--model", required=True)
    ap.add_argument("--sizes", default="2000,16000,48000")
    ap.add_argument("-n", type=int, default=3)
    ap.add_argument("--label", default="run")
    a = ap.parse_args()

    depths = [0.05, 0.25, 0.50, 0.75, 0.95]
    out = {"label": a.label, "model": a.model, "cells": []}
    for size in [int(s) for s in a.sizes.split(",")]:
        per_depth: dict[float, list[bool]] = {d: [] for d in depths}
        per_kind: dict[str, list[bool]] = {}
        for trial in range(a.n):
            prompt, facts = build_prompt(size, depths)
            try:
                reply = ask(a.base_url, a.model, prompt, list(facts))
            except Exception as e:
                print(f"  size={size} trial={trial+1} ERROR {e}", flush=True)
                continue
            for label, res in score(reply, facts).items():
                per_depth[res["depth"]].append(res["ok"])
                per_kind.setdefault(res["kind"], []).append(res["ok"])
                if not res["ok"]:
                    print(f"    MISS size={size} {label} {res['kind']} "
                          f"depth={res['depth']} got={res['got']!r}", flush=True)
        flat = [v for vs in per_depth.values() for v in vs]
        cell = {"size": size, "n_facts": len(flat),
                "exact": sum(flat), "rate": (sum(flat) / len(flat)) if flat else None,
                "by_depth": {str(d): (sum(v) / len(v) if v else None)
                             for d, v in per_depth.items()},
                "by_kind": {k: (sum(v) / len(v) if v else None)
                            for k, v in per_kind.items()}}
        out["cells"].append(cell)
        print(f"  size~{size:>6} tok: exact {cell['exact']}/{cell['n_facts']}"
              f"  by_depth={ {k: (round(v,2) if v is not None else None) for k,v in cell['by_depth'].items()} }",
              flush=True)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
