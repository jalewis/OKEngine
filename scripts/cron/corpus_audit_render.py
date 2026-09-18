"""Markdown rendering for the corpus integrity audit.

This module is presentation-only: it consumes the audit state and formatting policy and performs
no filesystem scanning or mutation.
"""
from __future__ import annotations


def render(
    state: dict,
    today: str,
    *,
    max_examples: int,
    recent_days: int,
    consumed_fields: dict[str, tuple[str, str]],
    max_entity_slug_len: int,
    min_alias_len: int,
    max_clusters: int,
) -> str:
    out = [
        "# Corpus integrity audit",
        "",
        f"Generated {today} by `corpus_audit.py` (no_agent). "
        f"{state['pages']} pages scanned, {state['parse_errors']} frontmatter parse errors.",
        "",
        "## Vocabulary drift (values outside the governing schema's enums)",
        "",
    ]
    drift = state["drift"]
    if not state["enums_declared"]:
        out += [
            "**UNDETECTABLE** — no governing schema declares resolvable `field_enums`, so "
            "top-level vocabulary drift cannot be measured on this vault (this is a WARN, "
            "not a pass). The nested `evidence[].direction` check still ran.",
            "",
        ]
    elif not drift:
        out += ["None — every audited value is inside its declared vocabulary.", ""]
    if drift:
        hot = sum(rec.get("recent", 0) for vals in drift.values() for rec in vals.values())
        if hot:
            out += [f"**⚠ ACTIVE PRODUCER REGRESSION: {hot} drifted value(s) on pages "
                    f"created/updated within {recent_days}d** — a lane is minting drift now "
                    f"(importers bypass the write path, okengine#237); find and fix the "
                    f"producer before backfilling.", ""]
        out += ["| Field | Out-of-enum value | Count | Recent(≤" + str(recent_days) + "d) | Example pages |",
                "|---|---|---|---|---|"]
        for field in sorted(drift):
            for val, rec in sorted(drift[field].items(), key=lambda kv: -kv[1]["count"]):
                ex = ", ".join(f"`{e}`" for e in rec["examples"])
                out.append(f"| `{field}` | `{val}` | {rec['count']} | {rec.get('recent', 0)} | {ex} |")
        out.append("")
    novel = state["novel"]
    if novel:
        out += [
            "## Novel values on extensible vocabularies (legal — but silent growth is pre-drift)",
            "",
            "| Field | Novel value | Count | Example pages |",
            "|---|---|---|---|",
        ]
        for field in sorted(novel):
            for val, rec in sorted(novel[field].items(), key=lambda kv: -kv[1]["count"]):
                ex = ", ".join(f"`{e}`" for e in rec["examples"])
                out.append(f"| `{field}` | `{val}` | {rec['count']} | {ex} |")
        out.append("")
    out += ["## Vocabulary the ingest minted (raw captures — okengine#594)", ""]
    raw_health = state.get("raw_capture_health") or {}
    minted = raw_health.get("minted_vocabulary") or {}
    if not raw_health.get("vocabulary_checked"):
        out += ["**UNDETECTABLE** — the `sources` schema declares no resolvable `field_enums`, "
                "so what the ingest lanes write into `raw/` cannot be checked against a "
                "vocabulary on this vault (a WARN, not a pass).", ""]
    elif not minted:
        out += ["None — every value the ingest lanes wrote is one the schema declares.", ""]
    else:
        out += ["An ingest lane writes `raw/` directly and so is never checked by the write "
                "path. A value it invents here is carried onto the source page, where it is "
                "either rejected or — worse — quietly replaced by whatever the compile model "
                "supplies instead. Fix the LANE; a backfill alone re-drifts on the next run.",
                "",
                "| Field | Minted value | Count | Legal? | Writing lane |",
                "|---|---|---:|---|---|"]
        for field in sorted(minted):
            for val, rec in minted[field].items():
                legal = ("extensible — legal, but undeclared" if rec["extensible"]
                         else "**no — closed enum**")
                lanes = ", ".join(f"`{lane}`" for lane in rec["lanes"]) or "—"
                out.append(f"| `{field}` | `{val}` | {rec['count']} | {legal} | {lanes} |")
        out.append("")
    out += ["## Dead fields (engine consumers of optional producers — okengine#221 class)", ""]
    out += ["| Field | Consumer | Candidates | Populated | Verdict |", "|---|---|---|---|---|"]
    for field, (cand_ns, consumer) in consumed_fields.items():
        cand, pop = state["candidates"][field], state["populated"][field]
        if cand == 0:
            verdict = "n/a (no candidate pages)"
        elif pop == 0:
            verdict = "**DEGRADED — consumer runs on fallback; producer missing?**"
        else:
            verdict = "OK"
        out.append(f"| `{field}` | {consumer} | {cand} (`{cand_ns}/`) | {pop} | {verdict} |")
    out += ["", "## Field coverage (schema-declared population ratios — okengine#264)", ""]
    cov = state.get("coverage") or {}
    if not state.get("coverage_declared"):
        out += ["**UNDETECTABLE** — no governing schema declares `coverage_fields`, so "
                "field-population coverage isn't tracked on this vault (a WARN, not a pass).", ""]
    elif not cov:
        out += ["Declared, but no pages of the declared type(s) exist yet.", ""]
    else:
        out += ["| Type.field | Populated | Total | Coverage | Floor | Verdict |",
                "|---|---:|---:|---:|---:|---|"]
        for name, r in cov.items():
            total, have = int(r.get("total") or 0), int(r.get("have") or 0)
            ratio, mn = float(r.get("ratio") or 0.0), r.get("min")
            floor = f"{mn:.0%}" if isinstance(mn, (int, float)) else "—"
            if isinstance(mn, (int, float)) and ratio < mn:
                verdict = f"**BELOW FLOOR — {total - have} page(s) missing the field**"
            else:
                verdict = "OK (complete)" if have == total else "OK"
            out.append(f"| `{name}` | {have} | {total} | {ratio:.1%} | {floor} | {verdict} |")
        out.append("")
    loop = state.get("prediction_loop") or {}
    total = int(loop.get("total") or 0)
    with_evidence = int(loop.get("with_evidence") or 0)
    terminal = int(loop.get("terminal") or 0)
    ungraded = int(loop.get("terminal_ungraded") or 0)
    open_primary = int(loop.get("open_primary") or 0)
    missing_method = int(loop.get("open_primary_missing_measurement_method") or 0)
    pct = lambda n, d: f"{100.0 * n / d:.1f}%" if d else "n/a"  # noqa: E731
    out += [
        "",
        "## Flat-vs-sharded source collisions",
        "",
    ]
    collisions = state.get("source_partition_collisions") or []
    out.append(
        f"**{len(collisions)}** exact article identity collision(s) across monthly and daily paths."
    )
    for paths in collisions[:max_examples]:
        out.append("- " + " ↔ ".join(f"`{path}`" for path in paths))
    out += [
        "",
        "## One subject, two spellings",
        "",
    ]
    near_dupes = state.get("near_duplicate_slugs") or []
    near_dupe_count = int(state.get("near_duplicate_slug_count") or 0)
    out.append(
        f"**{near_dupe_count}** name(s) exist at more than one spelling that differs only in "
        f"punctuation or spacing. Both copies are correctly filed, so no partition check sees "
        f"them; downstream, each is counted and ranked as its own subject."
    )
    for paths in near_dupes:
        out.append("- " + " ↔ ".join(f"`{path}`" for path in paths))
    out += [
        "",
        "## Human-review queue health",
        "",
    ]
    review = state.get("review_queue") or {}
    rq_total = int(review.get("total") or 0)
    rq_substantive = int(review.get("substantive") or 0)
    rq_fraction = float(review.get("fraction") or 0.0)
    rq_age = review.get("median_age_days")
    out += [
        "| Flagged | Corpus fraction | Substantive (>200 chars) | Median age |",
        "|---:|---:|---:|---:|",
        f"| {rq_total} | {rq_fraction:.1%} | {rq_substantive} | "
        f"{str(rq_age) + 'd' if rq_age is not None else 'n/a'} |",
        "",
    ]
    malformed = state.get("malformed_slugs") or {}
    out += [
        "## Malformed page basenames",
        "",
        f"**{int(malformed.get('count') or 0)}** entity page(s) have whitespace or exceed "
        f"{max_entity_slug_len} characters.",
    ]
    examples = malformed.get("examples") or []
    if examples:
        out += ["", *[f"- `{example}`" for example in examples]]
    integrity = state.get("body_integrity") or {}
    malformed_pages = int(integrity.get("malformed_heading_pages") or 0)
    malformed_occurrences = int(integrity.get("malformed_heading_occurrences") or 0)
    panel_pages = int(integrity.get("derived_panel_pages") or 0)
    panel_occurrences = int(integrity.get("derived_panel_occurrences") or 0)
    leaked_pages = int(integrity.get("leaked_frontmatter_pages") or 0)
    aggregator_pages = int(integrity.get("aggregator_as_source_pages") or 0)
    stale_rel_pages = int(integrity.get("stale_reliability_pages") or 0)
    badkey_pages = int(integrity.get("malformed_field_key_pages") or 0)
    retracted = int(integrity.get("retracted_without_successor") or 0)
    silent = int(integrity.get("subjects_left_silent") or 0)
    shape_bad = int(integrity.get("typed_shape_violations") or 0)
    shape_rules = int(integrity.get("typed_shape_rules") or 0)
    descriptor_pages = int(integrity.get("source_descriptor_pages") or 0)
    actor_publisher = int(integrity.get("actor_is_a_publisher_pages") or 0)
    actor_class = int(integrity.get("actor_class_contradiction_pages") or 0)
    overmatch = int(integrity.get("overmatching_identity_pages") or 0)
    alias_pages = int(integrity.get("overbroad_alias_pages") or 0)
    alias_terms = list(integrity.get("overbroad_alias_terms") or [])
    scalar_alias = int(integrity.get("scalar_alias_pages") or 0)
    out += [
        "",
        "## Body integrity",
        "",
        "| Defect | Pages | Occurrences | Example pages |",
        "|---|---:|---:|---|",
        f"| Malformed `## ##` headings | {malformed_pages} | {malformed_occurrences} | "
        + ", ".join(f"`{p}`" for p in integrity.get("malformed_heading_examples", [])) + " |",
        f"| Reader-derived panels authored as body H2 | {panel_pages} | {panel_occurrences} | "
        + ", ".join(f"`{p}`" for p in integrity.get("derived_panel_examples", [])) + " |",
        f"| Front-matter fragment leaked into body | {leaked_pages} | {leaked_pages} | "
        + ", ".join(f"`{p}`" for p in integrity.get("leaked_frontmatter_examples", [])) + " |",
        # A carrier holds data it does not own: crediting it as the source misattributes the
        # originator and invents a dataset that does not exist.
        f"| Aggregator credited as the source | {aggregator_pages} | {aggregator_pages} | "
        + ", ".join(f"`{p}`" for p in integrity.get("aggregator_as_source_examples", [])) + " |",
        # the page argues with itself and the reader is shown the weaker claim
        f"| Page `reliability` worse than its own evidence | {stale_rel_pages} | {stale_rel_pages} | "
        + ", ".join(f"`{p}`" for p in integrity.get("stale_reliability_examples", [])) + " |",
        # a judgment retired by nothing — and the subjects that fell silent as a result
        f"| Judgment retracted with no successor | {retracted} | {silent} subject(s) left with no "
        f"live judgment | " + ", ".join(f"`{p}`" for p in integrity.get("retracted_examples", []))
        + " |",
        # a frontmatter key is an identifier, not prose — these are agent invention, not vocabulary
        f"| Malformed frontmatter field key | {badkey_pages} | {badkey_pages} | "
        + ", ".join(f"`{p}`" for p in integrity.get("malformed_field_key_examples", [])) + " |",
        # the report's identity stamped onto the thing the report is ABOUT — a publisher minted as
        # an actor, or a real actor wearing a news outlet as its byline
        f"| Source descriptor on a non-source page | {descriptor_pages} | {descriptor_pages} | "
        + ", ".join(f"`{p}`" for p in integrity.get("source_descriptor_examples", [])) + " |",
        # a match count is not evidence: a common noun scores highest because it means least, and
        # the panels tie-break on it, so the emptiest pages take the top slots
        f"| Identity matched far more than it is cited for | {overmatch} | {overmatch} | "
        + ", ".join(f"`{p}`" for p in integrity.get("overmatching_identity_examples", [])) + " |",
        # a name the corpus publishes under is an outlet or a vendor -- an adversary does not file
        # its own advisories. Survives the repair for the row above, which strips its signature.
        f"| Actor whose identity is also a publisher | {actor_publisher} | {actor_publisher} | "
        + ", ".join(f"`{p}`" for p in integrity.get("actor_is_a_publisher_examples", [])) + " |",
        f"| Actor page whose evidence defines a non-actor | {actor_class} | {actor_class} | "
        + ", ".join(
            f"`{p}`" for p in integrity.get("actor_class_contradiction_examples", [])
        ) + " |",
        # an alias is a MATCH TERM: a short common token tags every page that contains it. Distinct
        # terms are counted separately from pages because a term is one decision however it spreads.
        f"| Over-broad alias (< {min_alias_len} chars) | {alias_pages} | {len(alias_terms)} distinct "
        f"term(s){' — ' + ', '.join(f'`{t}`' for t in alias_terms[:12]) if alias_terms else ''} | "
        + ", ".join(f"`{p}`" for p in integrity.get("overbroad_alias_examples", [])) + " |",
        # `aliases` is declared `list`; a scalar means the page was written around the write path
        f"| `aliases` is a scalar (declared `list`) | {scalar_alias} | {scalar_alias} | "
        "written outside the enforced write path |",
        # no declared rule = UNDETECTABLE, never a vacuous zero (the missing-key rule)
        (f"| Field shape wrong for its type | {shape_bad} | {shape_bad} | "
         + ", ".join(f"`{p}`" for p in integrity.get("typed_shape_examples", [])) + " |")
        if shape_rules else
        "| Field shape wrong for its type | — | — | **undetectable** — no `field_shapes.by_type` "
        "declared, so nothing is checked |",
    ]
    out += [
        "",
        "## Prediction feedback-loop engagement",
        "",
        "| Metric | Numerator | Denominator | Rate |",
        "|---|---:|---:|---:|",
        f"| Predictions carrying evidence | {with_evidence} | {total} | {pct(with_evidence, total)} |",
        f"| Terminal predictions left ungraded | {ungraded} | {terminal} | {pct(ungraded, terminal)} |",
        f"| Open primary predictions missing `measurement_method` | {missing_method} | "
        f"{open_primary} | {pct(missing_method, open_primary)} |",
    ]
    out += [
        "",
        "## Entity types outside the schema taxonomy",
        "",
    ]
    off = state.get("off_taxonomy") or {}
    if not off:
        out.append("**0** — every page's `type` is within its governing schema taxonomy.")
    else:
        n = sum(int(r.get("count") or 0) for r in off.values())
        out.append(
            f"**{n}** page(s) across **{len(off)}** type value(s) declare a `type` NOT in the "
            "governing schema (base ∪ pack + type_aliases). `strict_types` is OFF for this "
            "governing pack, so these bypass the validator and fragment entities across near-duplicate types:")
        out += ["", "| Type value | Pages | Recent | Examples |", "|---|---:|---:|---|"]
        rows = sorted(off.items(), key=lambda kv: -int(kv[1].get("count") or 0))
        for t, r in rows[:max_clusters]:
            # a run-on `type` (whole frontmatter collapsed into it) is itself a defect; truncate
            # for the table so one broken page can't blow up the report width.
            disp = t if len(t) <= 60 else t[:57] + "…"
            out.append(
                f"| `{disp}` | {int(r.get('count') or 0)} | {int(r.get('recent') or 0)} | "
                + ", ".join(f"`{e}`" for e in r.get("examples", [])) + " |")
        if len(rows) > max_clusters:
            out.append(f"| … {len(rows) - max_clusters} more type value(s) | | | |")
    out += [
        "",
        "## Same-entity fragmentation (shared-alias clusters)",
        "",
    ]
    frag = state.get("fragmentation") or []
    if not frag:
        out.append("**0** — no two entity pages share a normalized name/alias token.")
    else:
        out.append(
            f"**{len(frag)}** cluster(s) of entity pages sharing a normalized name/alias — likely "
            "one entity split across pages (entity resolution / canonical-assemble did not "
            "converge them). Consolidate onto a single canonical page:")
        for c in frag[:max_clusters]:
            aliases = ", ".join(f"`{k}`" for k in c.get("shared", [])) or "—"
            types = ", ".join(f"`{t}`" for t in c.get("types", [])) or "—"
            out += ["", f"- **{len(c['members'])} pages** sharing {aliases} (types: {types}):"]
            out += [f"    - `{m}`" for m in c["members"]]
    out += ["", "## Dangling path references (frontmatter path → a page that no longer exists)", ""]
    dangling = state.get("dangling_refs") or {}
    if not dangling:
        out += ["None — every bare `field: namespace/…` frontmatter path resolves to a page or "
                "shard.", ""]
    else:
        tot = sum(r["count"] for r in dangling.values())
        out += [
            f"**⚠ {tot} dangling reference(s)** — a page was moved/resharded without rewriting "
            "inbound BARE-path references (the assessment `subject:` join break, okengine#336). The "
            "consumer that joins on this field silently drops the row. Repoint the field, and ensure "
            "the mover ran the frontmatter-aware rewriter (`okf_migrate.make_path_rewriter`).",
            "",
            "| Field | Count | Examples (source → missing target) |",
            "|---|---|---|",
        ]
        for field in sorted(dangling, key=lambda f: -dangling[f]["count"]):
            r = dangling[field]
            out.append(f"| `{field}` | {r['count']} | {'; '.join(r['examples']) or '—'} |")
        out.append("")
    out += [
        "",
        "---",
        "*Drift here means agent-authored values wandered outside the sanctioned vocabulary; "
        "consumers may silently mis-bucket them. Enforcement at the write path is the fix "
        "(okengine#211/#217); this dashboard is the standing detector.*",
        "",
    ]
    return "\n".join(out)
