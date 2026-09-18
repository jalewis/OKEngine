"""Domain analysis for the corpus integrity audit.

Filesystem orchestration and Markdown rendering live outside this module. Dependencies are
supplied explicitly so callers and tests can substitute schema/policy boundaries without global
module coupling.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any, Callable


@dataclass(frozen=True)
class AuditContext:
    consumed_fields: dict[str, tuple[str, str]]
    evidence_direction_enum: Any
    evidence_direction_key: str
    judgment_kind_field: str
    max_entity_slug_len: int
    max_examples: int
    max_field_key_len: int
    min_identity_len: int
    novel: str
    overmatch_min_news: int
    overmatch_ratio: float
    prediction_terminal: Any
    prediction_ungraded: Any
    basis_grade_re: Any
    carrier_prose_re: Any
    field_key_re: Any
    frontmatter_re: Any
    leaked_frontmatter_re: Any
    shape_checks: Any
    alias_list: Callable[..., Any]
    actor_identity_error: Callable[..., Any]
    body_integrity_counts: Callable[..., Any]
    coverage_specs: Callable[..., Any]
    enum_rules: Callable[..., Any]
    frontmatter: Callable[..., Any]
    is_recent: Callable[..., Any]
    iter_pathrefs: Callable[..., Any]
    norm_identity: Callable[..., Any]
    overbroad_aliases: Callable[..., Any]
    skip: Callable[..., Any]
    slug_identity: Callable[..., Any]
    sources_enum_rules: Callable[..., Any]
    typed_shape_rules: Callable[..., Any]
    okf_migrate: Any
    provenance_lib: Any
    raw_capture_health: Callable[..., Any]
    schema_lib: Any


def audit(vault: Path, context: AuditContext) -> dict:
    CONSUMED_FIELDS = context.consumed_fields
    EVIDENCE_DIRECTION_ENUM = context.evidence_direction_enum
    EVIDENCE_DIRECTION_KEY = context.evidence_direction_key
    JUDGMENT_KIND_FIELD = context.judgment_kind_field
    MAX_ENTITY_SLUG_LEN = context.max_entity_slug_len
    MAX_EXAMPLES = context.max_examples
    MAX_FIELD_KEY_LEN = context.max_field_key_len
    MIN_IDENTITY_LEN = context.min_identity_len
    NOVEL = context.novel
    OVERMATCH_MIN_NEWS = context.overmatch_min_news
    OVERMATCH_RATIO = context.overmatch_ratio
    PREDICTION_TERMINAL = context.prediction_terminal
    PREDICTION_UNGRADED = context.prediction_ungraded
    _BASIS_GRADE_RE = context.basis_grade_re
    _CARRIER_PROSE_RE = context.carrier_prose_re
    _FIELD_KEY_RE = context.field_key_re
    _FM_RE = context.frontmatter_re
    _LEAKED_FRONTMATTER_RE = context.leaked_frontmatter_re
    _SHAPE_CHECKS = context.shape_checks
    _alias_list = context.alias_list
    _actor_identity_error = context.actor_identity_error
    _body_integrity_counts = context.body_integrity_counts
    _coverage_specs = context.coverage_specs
    _enum_rules = context.enum_rules
    _frontmatter = context.frontmatter
    _is_recent = context.is_recent
    _iter_pathrefs = context.iter_pathrefs
    _norm_identity = context.norm_identity
    _overbroad_aliases = context.overbroad_aliases
    _skip = context.skip
    _slug_identity = context.slug_identity
    _sources_enum_rules = context.sources_enum_rules
    _typed_shape_rules = context.typed_shape_rules
    okf_migrate = context.okf_migrate
    provenance_lib = context.provenance_lib
    raw_capture_health = context.raw_capture_health
    schema_lib = context.schema_lib

    """Walk the corpus once; return the measured state (pure, testable)."""
    wiki = vault / "wiki"
    # drift/novel: [field][bad_value] -> {"count": n, "examples": [rel, ...]}
    _bucket = lambda: defaultdict(lambda: {"count": 0, "recent": 0, "examples": []})  # noqa: E731
    drift: dict = defaultdict(_bucket)   # strict enums — the write path would reject these now
    novel: dict = defaultdict(_bucket)   # extensible enums — legal, but silent growth = pre-drift
    populated: dict = {f: 0 for f in CONSUMED_FIELDS}
    candidates: dict = {f: 0 for f in CONSUMED_FIELDS}
    schema_cache: dict = {}
    shape_cache: dict = {}
    rules_cache: dict = {}
    cov_cache: dict = {}     # govdir -> [(type, field, min_ratio), ...] from schema.coverage_fields
    # (type, field) -> {"total", "have", "min"} — schema-declared field-population coverage (#264)
    coverage: dict = defaultdict(lambda: {"total": 0, "have": 0, "min": None})
    coverage_declared = False
    types_cache: dict = {}   # govdir -> set of valid type names (base ∪ pack types + type_aliases)
    # type value -> occurrences of a page whose `type` is outside the governing taxonomy
    off_taxonomy: dict = defaultdict(lambda: {"count": 0, "recent": 0, "examples": []})
    # alias-fragmentation: normalized identity token -> set of entity rels claiming it, and
    # per-entity metadata for the cluster report.
    identity_index: dict[str, set] = defaultdict(set)
    entity_meta: dict[str, dict] = {}
    enums_declared = False
    pages = parse_errors = 0
    prediction_loop = {
        "total": 0,
        "with_evidence": 0,
        "terminal": 0,
        "terminal_ungraded": 0,
        "open_primary": 0,
        "open_primary_missing_measurement_method": 0,
    }
    source_signatures: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    review_ages: list[int] = []
    review_total = review_substantive = 0
    malformed_slugs: list[str] = []
    malformed_slug_count = 0
    body_integrity = {
        "malformed_heading_occurrences": 0,
        "malformed_heading_pages": 0,
        "malformed_heading_examples": [],
        "derived_panel_occurrences": 0,
        "derived_panel_pages": 0,
        "derived_panel_examples": [],
        "leaked_frontmatter_pages": 0,
        "leaked_frontmatter_examples": [],
        "aggregator_as_source_pages": 0,
        "aggregator_as_source_examples": [],
        "stale_reliability_pages": 0,
        "stale_reliability_examples": [],
        "malformed_field_key_pages": 0,
        "malformed_field_key_examples": [],
        "retracted_without_successor": 0,
        "retracted_examples": [],
        "judgments_left_silent": 0,
        "subjects_left_silent": 0,
        "judgment_kind_field": "",
        "typed_shape_violations": 0,
        "typed_shape_examples": [],
        "typed_shape_rules": 0,
        "source_descriptor_pages": 0,
        "source_descriptor_examples": [],
        "overmatching_identity_pages": 0,
        "overmatching_identity_examples": [],
        "actor_is_a_publisher_pages": 0,
        "actor_is_a_publisher_examples": [],
        "actor_class_contradiction_pages": 0,
        "actor_class_contradiction_examples": [],
        "overbroad_alias_pages": 0,
        "overbroad_alias_terms": [],
        "overbroad_alias_examples": [],
        "scalar_alias_pages": 0,
    }
    # An aggregator/repository is a CARRIER, not an originator: data it holds is owned by whoever
    # produced it, and the source record already models that (`publisher` = originator,
    # `retrieved_via` = carrier). Body prose crediting the carrier — "identified in the <X> dataset"
    # — misattributes the originator and invents a dataset that does not exist.
    #
    # Collected in the SAME pass as everything else: carriers come from the corpus's own
    # `retrieved_via` values (so this stays domain-agnostic — no vendor name in the engine), and
    # candidate prose is matched generically, then filtered against those carriers after the loop.
    # A pre-pass would mean a second full rglob + parse of every source record.
    carriers: set[str] = set()
    carrier_prose: list[tuple[str, str]] = []      # (captured name, page rel)

    # AN ACTOR THAT IS ALSO A PUBLISHER (okengine#589 follow-up). `publisher` + `source_kind` on a
    # non-source page catches a report's identity stamped on its subject -- but only while those
    # fields are still there. Stripping them (the repair for that defect) removes the SIGNATURE and
    # leaves the page: `entities/a/anthropic` stayed `type: actor` with the detector reporting 0.
    #
    # This asks a question the repair cannot erase: is this actor's identity one the corpus uses as
    # a PUBLISHER? A name that publishes reporting is an outlet or a vendor; an adversary does not
    # file its own advisories. Both directions count, because a pack may model either:
    #   - a `type: publisher` ENTITY page under the same identity, or
    #   - the same name used as `publisher:` on source records (>=1 use; a one-off is enough,
    #     the corpus does not accidentally attribute a report to an adversary).
    # Engine-side: `publisher` is a base common_optional field and `source` a base type.
    publisher_identities: set[str] = set()         # identities the corpus PUBLISHES under
    actor_identities: list[tuple[str, str]] = []   # (normalized identity, page rel)

    existing_paths: set[str] = set()   # every real page (no .md), for dangling-ref resolution
    path_refs: list[tuple[str, str, str]] = []   # (field, target, source_rel) — bare path refs
    slug_variants: dict[tuple[str, str], set[str]] = defaultdict(set)   # (ns, norm slug) -> rels

    for p in sorted(wiki.rglob("*.md")):
        rel = p.relative_to(wiki)
        existing_paths.add(rel.as_posix()[:-3])   # BEFORE _skip: a skipped page is still a valid target
        if _skip(rel):
            continue
        _norm_slug = _slug_identity(p.stem)
        if _norm_slug:
            slug_variants[(rel.parts[0], _norm_slug)].add(rel.as_posix()[:-3])
        fm = _frontmatter(p)
        if fm is None:
            continue
        pages += 1
        if fm == {}:
            parse_errors += 1
            continue
        for _field, _target in _iter_pathrefs(fm):
            path_refs.append((_field, _target, str(rel)))
        try:
            page_text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            page_text = ""
        body = _FM_RE.sub("", page_text, count=1)
        _badkeys = [str(k) for k in fm
                    if not _FIELD_KEY_RE.match(str(k)) or len(str(k)) > MAX_FIELD_KEY_LEN]
        if _badkeys:
            body_integrity["malformed_field_key_pages"] += 1
            if len(body_integrity["malformed_field_key_examples"]) < MAX_EXAMPLES:
                body_integrity["malformed_field_key_examples"].append(
                    f"{rel} → `{_badkeys[0][:60]}`")
        if fm.get("retrieved_via"):
            carriers.add(str(fm["retrieved_via"]).strip())
        # A page-level Admiralty `reliability` that is WORSE than the grade its own
        # `auto_verified_basis` cites. Admiralty reliability grades a SOURCE; when it is stamped
        # onto a knowledge page it is a snapshot of whatever produced that page, and it does not
        # move as evidence accumulates. The trust strip then shows the stale, lower grade beside a
        # basis naming A-grade authorities — the page argues with itself, and the weaker claim is
        # the one the reader sees.
        _rel = str(fm.get("reliability") or "").strip().upper()
        _best = min((g for _, g in _BASIS_GRADE_RE.findall(str(fm.get("auto_verified_basis") or ""))),
                    default=None)
        if _rel and _best and _rel > _best:          # 'C' > 'A': a worse grade than the evidence
            body_integrity["stale_reliability_pages"] += 1
            if len(body_integrity["stale_reliability_examples"]) < MAX_EXAMPLES:
                body_integrity["stale_reliability_examples"].append(f"{rel} → {_rel} vs {_best}")
        for _cm in _CARRIER_PROSE_RE.finditer(body):
            carrier_prose.append((_cm.group(1).strip(), str(rel)))
        if _LEAKED_FRONTMATTER_RE.match(body):
            body_integrity["leaked_frontmatter_pages"] += 1
            if len(body_integrity["leaked_frontmatter_examples"]) < MAX_EXAMPLES:
                body_integrity["leaked_frontmatter_examples"].append(str(rel))
        malformed_n, derived_n = _body_integrity_counts(body)
        if malformed_n:
            body_integrity["malformed_heading_occurrences"] += malformed_n
            body_integrity["malformed_heading_pages"] += 1
            if len(body_integrity["malformed_heading_examples"]) < MAX_EXAMPLES:
                body_integrity["malformed_heading_examples"].append(str(rel))
        if derived_n:
            body_integrity["derived_panel_occurrences"] += derived_n
            body_integrity["derived_panel_pages"] += 1
            if len(body_integrity["derived_panel_examples"]) < MAX_EXAMPLES:
                body_integrity["derived_panel_examples"].append(str(rel))

        parts = rel.parts
        if parts and parts[0] == "sources" and len(parts) in (4, 5):
            title = str(fm.get("title") or fm.get("name") or "").strip().casefold()
            publisher = str(fm.get("publisher") or "").strip().casefold()
            published = str(fm.get("published") or "")[:10]
            if title and publisher and published:
                source_signatures[(title, publisher, published)].append(str(rel))

        if fm.get("needs_review") is True:
            review_total += 1
            if len(body.strip()) > 200:
                review_substantive += 1
            stamp = fm.get("last_updated") or fm.get("updated") or fm.get("created")
            s = stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp or "")
            try:
                review_ages.append(max(0, (date.today() - date.fromisoformat(s[:10])).days))
            except (TypeError, ValueError):
                pass

        if rel.parts and rel.parts[0] == "entities" and (
            any(ch.isspace() for ch in p.stem) or len(p.stem) > MAX_ENTITY_SLUG_LEN
        ):
            malformed_slug_count += 1
            if len(malformed_slugs) < MAX_EXAMPLES:
                malformed_slugs.append(str(rel))

        ns = str(rel.parent) if rel.parent != Path(".") else ""
        govdir = schema_lib._governing_dir(vault, ns)
        if govdir not in schema_cache:
            schema_cache[govdir] = schema_lib.merged_schema(vault, ns)
            rules_cache[govdir] = _enum_rules(schema_cache[govdir])
            cov_cache[govdir] = _coverage_specs(schema_cache[govdir])
            shape_cache[govdir] = _typed_shape_rules(schema_cache[govdir])
            sch = schema_cache[govdir]
            types_cache[govdir] = set(sch.get("types") or {}) | set(sch.get("type_aliases") or {})
        rules = rules_cache[govdir]
        if rules:
            enums_declared = True
        # per-type SHAPE check: a field can mean a number on one type and a band on another
        for (_ty, _fld), _shape in (shape_cache.get(govdir) or {}).items():
            if str(fm.get("type") or "") != _ty or _fld not in fm:
                continue
            if not _SHAPE_CHECKS[_shape](fm[_fld]):
                body_integrity["typed_shape_violations"] += 1
                if len(body_integrity["typed_shape_examples"]) < MAX_EXAMPLES:
                    body_integrity["typed_shape_examples"].append(
                        f"{rel} → {_fld}={fm[_fld]!r} is not {_shape}")

        # PRODUCER-REGRESSION signal (okengine#237): a drifted value on a RECENTLY created/
        # updated page means a producer is minting drift NOW (importers bypass the write path)
        # — not legacy data awaiting backfill. Rendered as its own column + a headline alert.
        page_recent = _is_recent(fm)

        # A tombstoned page is intentionally superseded (a dedup/merge loser pointing at its
        # canonical) — it is the RESOLUTION of these two defects, not an instance. Counting it
        # would mean tombstoning never clears the signal (a merged dup keeps its old alias/type).
        tombstoned = str(fm.get("status") or "").strip().lower() == "tombstoned"

        # 0. type OUTSIDE the governing taxonomy. strict_types defaults OFF, so a permissive pack
        # does not enforce its own type taxonomy at the write path — STIX-style names (e.g.
        # `threat-actor_group`, `threat_actor_family`) slip in and fragment an entity across
        # near-duplicate types. Base ∪ pack types (+ type_aliases) are the sanctioned set.
        ptype = str(fm.get("type") or "").strip()
        if ptype == "actor" and not tombstoned:
            title = str(fm.get("title") or fm.get("name") or p.stem).strip()
            contradiction = _actor_identity_error(title, body)
            if contradiction:
                body_integrity["actor_class_contradiction_pages"] += 1
                if len(body_integrity["actor_class_contradiction_examples"]) < MAX_EXAMPLES:
                    body_integrity["actor_class_contradiction_examples"].append(
                        f"{rel} → {contradiction}"
                    )
        valid_types = types_cache.get(govdir) or set()
        if not tombstoned and ptype and valid_types and ptype not in valid_types:
            rec = off_taxonomy[ptype]
            rec["count"] += 1
            rec["recent"] += 1 if page_recent else 0
            if len(rec["examples"]) < MAX_EXAMPLES:
                rec["examples"].append(str(rel))

        # 0d. SOURCE DESCRIPTOR ON A NON-SOURCE PAGE (okengine#589). `publisher` and `source_kind`
        # describe a SOURCE RECORD -- who published an item and what kind of item it is. Carried by a
        # page that is not a source, they are the report's identity stamped onto the thing the report
        # is ABOUT: `entities/a/anthropic` was minted `type: actor` with `publisher: Anthropic` and
        # `source_kind: report`, i.e. a company promoted to a threat actor by its own byline. The
        # inverse is worse and also present: `unc3886` is a genuine tracked actor wearing a news
        # outlet as its publisher.
        #
        # Both fields together, never either alone -- `publisher` alone is legitimately carried by 97
        # pages here (a vendor entity has a publisher), and flagging those would drown the signal.
        # Fully engine-side: `source` is a BASE type and both fields are base `common_optional`, so
        # this needs no pack input; type_aliases are resolved so a pack's own spelling of "source"
        # is not misread as a non-source.
        if not tombstoned and ptype:
            _canon = schema_lib.canonical_type(schema_cache[govdir], ptype)
            if (_canon != "source" and fm.get("publisher") not in (None, "", [], {})
                    and str(fm.get("source_kind") or "").strip()):
                body_integrity["source_descriptor_pages"] += 1
                if len(body_integrity["source_descriptor_examples"]) < MAX_EXAMPLES:
                    body_integrity["source_descriptor_examples"].append(
                        f"{rel} → {ptype} published by “{str(fm.get('publisher'))[:40]}”")

        # 0e. OVER-BROAD ALIAS (okengine#589). An alias is a MATCH TERM, so a short common token
        # tags every page that happens to contain it. See MIN_ALIAS_LEN for why the floor is 4.
        if not tombstoned:
            if fm.get("aliases") is not None and not isinstance(fm.get("aliases"), list):
                body_integrity["scalar_alias_pages"] += 1
            _broad = _overbroad_aliases(fm.get("aliases"))
            if _broad:
                body_integrity["overbroad_alias_pages"] += 1
                body_integrity["overbroad_alias_terms"].extend(_broad)
                if len(body_integrity["overbroad_alias_examples"]) < MAX_EXAMPLES:
                    body_integrity["overbroad_alias_examples"].append(
                        f"{rel} → {', '.join(sorted(_broad))}")

        # 0f. collect the two sides of the actor-is-a-publisher join (okengine#589 follow-up).
        if not tombstoned:
            _canon_t = schema_lib.canonical_type(schema_cache[govdir], ptype) if ptype else ""
            if _canon_t == "source" and fm.get("publisher") not in (None, "", [], {}):
                _k = _norm_identity(fm.get("publisher"))
                if len(_k) >= MIN_IDENTITY_LEN:
                    publisher_identities.add(_k)
            elif ptype == "publisher":
                for _src in (fm.get("title"), fm.get("name")):
                    _k = _norm_identity(_src)
                    if len(_k) >= MIN_IDENTITY_LEN:
                        publisher_identities.add(_k)
            elif ptype == "actor":
                # BOTH title and name, like the fragmentation index and the publisher side above.
                # `title or name` ignores `name` whenever a title exists, so a page titled
                # "Intruder Group" whose name is "Intruder" would never join the publisher called
                # "Intruder" -- the identity that actually collides is the one that gets dropped.
                for _src in (fm.get("title"), fm.get("name")):
                    _k = _norm_identity(_src)
                    if len(_k) >= MIN_IDENTITY_LEN:
                        actor_identities.append((_k, str(rel)))

        # 0g. AN IDENTITY THAT MATCHES MORE THAN IT IS CITED FOR (okengine#591).
        # `recent_news` is an UNVALIDATED MATCH COUNT: a lane counts articles whose text contains the
        # entity's name. Nobody checked those articles are about it. A common noun therefore scores
        # highest precisely BECAUSE it is meaningless -- `Attacker` carried 18 news on 1 cited source
        # while `Scattered Spider`, a heavily-reported real actor, carried 9 on 35.
        #
        # That would be a curiosity if nothing consumed it, but the cockpit's actor panels sort on
        # `news_last_seen` and TIE-BREAK on `recent_news`, so the ranking is an amplifier pointed at
        # the pages with the least evidence behind them. 0.2% of actor pages are junk and they take
        # the top slots.
        #
        # The ratio separates the two populations completely on the measured corpus: every genuine
        # actor sits below 1.0, every generic noun at or above 2.0. The floor is set at 3.0 -- well
        # clear of the highest real actor (1.4) -- because a detector that clips the top of the
        # legitimate range is one that gets switched off.
        #
        # Engine-side and domain-agnostic: it names no vocabulary, only the shape "matched a lot,
        # cited by little". A pack that does not populate `recent_news` reports nothing.
        if not tombstoned and ptype:
            try:
                _news = int(fm.get("recent_news") or 0)
            except (TypeError, ValueError):
                _news = 0                       # a non-numeric count is not evidence of anything
            _cited = fm.get("sources")
            _cited_n = len(_cited) if isinstance(_cited, list) else (1 if _cited else 0)
            if _news >= OVERMATCH_MIN_NEWS and _news >= OVERMATCH_RATIO * max(_cited_n, 1):
                body_integrity["overmatching_identity_pages"] += 1
                if len(body_integrity["overmatching_identity_examples"]) < MAX_EXAMPLES:
                    body_integrity["overmatching_identity_examples"].append(
                        f"{rel} → {_news} news / {_cited_n} cited source(s)")

        # 0c. schema-declared field coverage (okengine#264): the population ratio of each (type,
        # field) the governing schema lists in `coverage_fields`. A sparsely-populated field (a KEV
        # backlog whose cvss_base never got backfilled) is a standing dashboard row here instead of a
        # per-review rediscovery. Engine-agnostic — the pack names the fields; tombstones excluded.
        cov_specs = cov_cache.get(govdir) or []
        if cov_specs:
            coverage_declared = True
            if not tombstoned and ptype:
                for ct, cf, mn in cov_specs:
                    if ct != ptype:
                        continue
                    rec = coverage[(ct, cf)]
                    rec["total"] += 1
                    rec["min"] = mn
                    if fm.get(cf) not in (None, "", [], {}):
                        rec["have"] += 1

        # 0b. entity identity tokens (name/title/aliases) -> the alias-fragmentation index. An
        # exact normalized alias shared by >1 entity page is the strong signal that entity
        # resolution / canonical-assemble failed to converge them (the Gentlemen repro).
        if not tombstoned and rel.parts and rel.parts[0] == "entities":
            keys = set()
            for src in (fm.get("name"), fm.get("title")):
                k = _norm_identity(src)
                if len(k) >= MIN_IDENTITY_LEN:
                    keys.add(k)
            # _alias_list, not `isinstance(..., list)`: that skip meant a page whose `aliases` is a
            # scalar never entered the index at all, so the pages with a malformed field were the
            # ones exempt from fragmentation detection.
            for a in _alias_list(fm.get("aliases")):
                k = _norm_identity(a)
                if len(k) >= MIN_IDENTITY_LEN:
                    keys.add(k)
            if keys:
                entity_meta[str(rel)] = {"type": ptype, "keys": keys}
                for k in keys:
                    identity_index[k].add(str(rel))

        # 1a. top-level vocabulary check against the governing schema's field_enums
        for field in rules:
            val = fm.get(field)
            verdict = provenance_lib.classify_value(field, val, rules)
            if verdict is None:
                continue
            rec = (novel if verdict == NOVEL else drift)[field][val]
            rec["count"] += 1
            rec["recent"] += 1 if page_recent else 0
            if len(rec["examples"]) < MAX_EXAMPLES:
                rec["examples"].append(str(rel))

        # 1b. nested evidence[].direction (hardcoded until #211 — see constant docstring)
        ev = fm.get("evidence")
        if isinstance(ev, list):
            for item in ev:
                if not isinstance(item, dict):
                    continue
                d = item.get("direction")
                if isinstance(d, str) and d not in EVIDENCE_DIRECTION_ENUM:
                    rec = drift[EVIDENCE_DIRECTION_KEY][d]
                    rec["count"] += 1
                    rec["recent"] += 1 if page_recent else 0
                    if len(rec["examples"]) < MAX_EXAMPLES:
                        rec["examples"].append(str(rel))

        # 1c. prediction feedback-loop engagement. Keep the detector generic: it reads the
        # engine's core prediction envelope and treats measurement_method as an optional maturity
        # signal, never a conformance requirement.
        if str(fm.get("type") or "").strip() == "prediction":
            prediction_loop["total"] += 1
            if isinstance(ev, list) and ev:
                prediction_loop["with_evidence"] += 1
            status = str(fm.get("status") or "").strip().lower()
            if status in PREDICTION_TERMINAL:
                prediction_loop["terminal"] += 1
                if status in PREDICTION_UNGRADED:
                    prediction_loop["terminal_ungraded"] += 1
            primary = any(k in fm for k in ("made_on", "horizon", "resolves_by"))
            if status == "open" and primary:
                prediction_loop["open_primary"] += 1
                if not str(fm.get("measurement_method") or "").strip():
                    prediction_loop["open_primary_missing_measurement_method"] += 1

        # 2. dead-field population over candidate namespaces
        top = rel.parts[0] if rel.parts else ""
        sub = rel.parts[1] if len(rel.parts) > 2 else ""  # walk-up subdomain: <sub>/<ns>/page
        for field, (cand_ns, _consumer) in CONSUMED_FIELDS.items():
            if top == cand_ns or sub == cand_ns:
                candidates[field] += 1
                v = fm.get(field)
                if v not in (None, "", [], {}):
                    populated[field] += 1

    # Per-shared-key clustering (NOT transitive union-find): each normalized alias/name claimed by
    # >1 entity page is one cluster. Transitive merging over-connects — a single page listing many
    # aliases bridges genuinely distinct actors into a blob (OilRig+APT41+Kimsuky) and destroys the
    # signal. An EXACT shared normalized alias is the high-precision "same entity" join. Clusters
    # with the identical member set (page shares both name and an alias) are merged, keys unioned.
    by_members: dict[tuple, dict] = {}
    fragmentation = []
    for k, rels in sorted(identity_index.items()):
        if len(rels) < 2:
            continue
        members = tuple(sorted(rels))
        if members in by_members:
            by_members[members]["shared"].append(k)
            continue
        entry = {
            "members": list(members),
            "shared": [k],
            "types": sorted({entity_meta[m]["type"] for m in members if entity_meta[m]["type"]}),
        }
        by_members[members] = entry
        fragmentation.append(entry)
    for e in fragmentation:
        e["shared"] = sorted(e["shared"])
    fragmentation.sort(key=lambda c: (-len(c["members"]), c["members"][0]))

    # DANGLING PATH REFERENCES (#336) — bare frontmatter paths whose target no longer exists: a
    # move/reshard that never rewrote the reference (the assessment `subject:` join break). Scoped to
    # real top-level namespaces so an arbitrary slashed string isn't misread as a page reference; a
    # target that names a shard/dir (a prefix of some page) resolves too.
    namespaces = {pp.split("/", 1)[0] for pp in existing_paths}
    existing_dirs: set[str] = set()
    for pp in existing_paths:
        parts = pp.split("/")
        for i in range(1, len(parts)):
            existing_dirs.add("/".join(parts[:i]))
    # Report carrier-credited prose only where the captured name is one the corpus itself declares
    # as a `retrieved_via` carrier — so an ordinary sentence about a real dataset is not flagged,
    # and the engine never has to name a repository.
    _seen_carrier_pages: set[str] = set()
    _norm_carriers = {c.casefold() for c in carriers if c}
    for _name, _rel in carrier_prose:
        if _name.casefold() not in _norm_carriers or _rel in _seen_carrier_pages:
            continue
        _seen_carrier_pages.add(_rel)
        body_integrity["aggregator_as_source_pages"] += 1
        if len(body_integrity["aggregator_as_source_examples"]) < MAX_EXAMPLES:
            body_integrity["aggregator_as_source_examples"].append(f"{_rel} → “{_name}”")

    # Resolved AFTER the loop: a publisher page or source record may be walked long after the actor
    # page that collides with it, so deciding during the walk would depend on directory order.
    for _identity, _rel in sorted(set(actor_identities)):
        if _identity not in publisher_identities:
            continue
        body_integrity["actor_is_a_publisher_pages"] += 1
        if len(body_integrity["actor_is_a_publisher_examples"]) < MAX_EXAMPLES:
            body_integrity["actor_is_a_publisher_examples"].append(f"{_rel} → “{_identity}”")

    # Resolved AFTER the walk for the same reason as the publisher collision above: the two
    # spellings of one name are usually in different shards, so a during-the-walk decision would
    # depend on directory order. Sorted shallowest-first, which is the older seat often enough
    # to be a useful hint about which copy came first -- but NOT a merge instruction: which
    # spelling is canonical is an identity judgment, and this only reports the pair.
    near_duplicate_slugs = [
        sorted(rels, key=lambda rel: (len(Path(rel).parts), rel))
        for _key, rels in sorted(slug_variants.items())
        if len(rels) > 1
    ]

    body_integrity["typed_shape_rules"] = sum(len(v) for v in shape_cache.values())
    # The DISTINCT terms are what an operator acts on -- one bad alias on nine pages is one decision,
    # not nine -- so the page count and the term count are reported as separate numbers.
    body_integrity["overbroad_alias_terms"] = sorted(set(body_integrity["overbroad_alias_terms"]))

    # RETRACTED-WITHOUT-SUCCESSOR (okengine#563). A judgment is retired only BY another judgment:
    # attribution changes when EVIDENCE changes, and the superseded_by chain IS that history. A
    # record marked superseded with no successor retracted a live claim on the strength of nothing.
    # One lane run did that to 344 subjects in a single pass -- created 0, reported success -- and
    # nothing noticed for hours because no detector watched the resulting state.
    _judgment_status = {"superseded", "retired", "tombstoned"}

    def _subject_key(ref: str) -> str:
        """Identify a subject by (namespace, slug), ignoring the SHARD segments between them.

        A partitioned namespace re-files pages as it grows — `entities/b/blackcat-alphv` becomes
        `entities/b/l/blackcat-alphv` on a second-letter reshard — and records written on either
        side of that move cite different spellings of the SAME page. Compared literally they read
        as two subjects, so an actor with a live judgment under one spelling and a retraction under
        the other looks silent while nothing is actually missing. Measured literally this vault
        reported a silent judgment for an actor holding two live ones.
        """
        parts = [p for p in str(ref).strip().removesuffix(".md").split("/") if p]
        if len(parts) < 2:
            return "/".join(parts)
        return f"{parts[0]}/{parts[-1]}"

    def _subjects(value) -> list:
        """A `subject` may be a scalar OR a list — both spellings are live in the corpus.
        Stringifying the list ("['entities:x']") makes it un-matchable against the scalar spelling
        of the SAME subject, so a subject retracted in one form and re-asserted in the other reads
        as silent. Compare element-wise, each normalized to its shard-independent identity."""
        items = value if isinstance(value, list) else [value]
        return [_subject_key(s) for s in (str(v).strip() for v in items if v is not None) if s]

    # GRAIN. A subject usually carries SEVERAL independent judgments -- on a live vault, an actor
    # holds separate records for its origin and for its identity scope. Counting silence per
    # SUBJECT hides that: an actor whose origin judgment was retracted still looks "covered"
    # because an unrelated identity judgment survives. Measured per subject it reported 1; per
    # (subject, type, kind) the same corpus reported 38, including five actors left with no origin
    # attribution at all. The kind FIELD is pack vocabulary, so the engine takes it as config and
    # never guesses a name -- and says so when it has none, rather than passing vacuously.
    _kind_field = JUDGMENT_KIND_FIELD
    _live: set = set()
    _retracted: list = []
    for _p in wiki.rglob("*.md"):
        _fm = _frontmatter(_p)
        if not _fm:
            continue
        _subj = _subjects(_fm.get("subject"))
        if not _subj:
            continue
        _kind = str(_fm.get(_kind_field) or "") if _kind_field else ""
        _keys = {(s, str(_fm.get("type") or ""), _kind) for s in _subj}
        _st = str(_fm.get("status") or "").strip().lower()
        if _st not in _judgment_status:
            _live |= _keys
        elif not str(_fm.get("superseded_by") or "").strip():
            _retracted.append((str(_p.relative_to(wiki)), _subj, _keys))
    # silent = a judgment named by a retraction that nothing live re-asserts at the same grain
    _silent = {k for _, _, keys in _retracted for k in keys} - _live
    body_integrity["retracted_without_successor"] = len(_retracted)
    body_integrity["judgments_left_silent"] = len(_silent)
    # kept for continuity with the coarser prior metric; it is a FLOOR, never the real number
    body_integrity["subjects_left_silent"] = len({s for s, _t, _k in _silent})
    body_integrity["judgment_kind_field"] = _kind_field
    # warn only when the grain could actually change a number — with nothing retracted it cannot
    if _retracted and not _kind_field:
        body_integrity["judgment_grain_warning"] = (
            "no CORPUS_AUDIT_JUDGMENT_KIND_FIELD configured — silence is measured per "
            "(subject, type) only. A subject carrying several judgment KINDS under one type will "
            "under-report: a retracted judgment reads as covered whenever any sibling survives.")
    for _rel, _subj, _keys in _retracted[:MAX_EXAMPLES]:
        _k = sorted({k for _s, _t, k in _keys if k})
        body_integrity["retracted_examples"].append(
            f"{_rel} → subject {', '.join(_subj)}" + (f" [{', '.join(_k)}]" if _k else ""))

    # PARTITION MISFILED — a page off its canonical seat that has NOT been duplicated yet.
    #
    # DUPLICATES are deliberately NOT counted here: `deployment_checks.check_partition_dups()`
    # already does exactly that, across the same namespaces, and better (it also skips tombstoned
    # pages, which are intentionally left at a stale path). A second implementation of one check is
    # how the two drift — this file's first attempt at it lacked the structural-page skip that the
    # existing lane had carried all along, and a converge step built on that reading deleted ~1,600
    # generated INDEX pages in a single pass. One definition, one place.
    #
    # What is genuinely NOT covered elsewhere is the pre-duplication signal: check_partition_dups
    # fires only once a slug sits at 2+ paths, i.e. after the writer/drain loop has already run
    # twice. A single copy in the wrong place is the same defect one drain-run earlier, and it is
    # cheap to fix then (a move) versus later (a merge with contradictory field values).
    #
    # Only computed for strategies whose seat depends on the SLUG ALONE. by-date and by-type read
    # frontmatter this walk does not retain, and passing `{}` would make canonical_key fall back and
    # report every dated source as misfiled. Rather than emit a confidently wrong number, the check
    # NAMES the strategies it did not verify.
    _SLUG_ONLY_STRATEGIES = {"by-letter"}
    _part_ns: dict = {}
    _seats: dict = defaultdict(list)
    partition_misfiled: list = []
    misfiled_unchecked: set = set()

    def _strategy(ns: str) -> str:
        if ns not in _part_ns:
            try:                                   # private, same codebase: is_partitioned only
                cfg = okf_migrate._partition_cfg(  # returns a bool, and the STRATEGY is the point
                    okf_migrate._governing_schema(vault, ns), ns)
                _part_ns[ns] = str((cfg or {}).get("strategy") or "flat")
            except Exception:
                _part_ns[ns] = "flat"
        return _part_ns[ns]

    # STRUCTURAL pages exist once PER DIRECTORY by design — build_index_tree writes an INDEX.md at
    # every level it wants an agent to traverse. They are not content and they do not have a
    # canonical seat, so grouping them by basename makes every shard's INDEX.md look like one
    # enormous collision. Treating that as a duplicate set is destructive: a converge step keyed on
    # this reading deleted ~1,600 index pages across every namespace in one pass.
    _STRUCTURAL = {"INDEX", "index", "log", "BUNDLE", "HEALTH", "AGENTS", "_about"}
    for rel in sorted(existing_paths):
        _parts = rel.split("/")
        if len(_parts) < 2:
            continue
        _stem = _parts[-1]
        if _stem in _STRUCTURAL or _stem.startswith("INDEX-p") or _stem.startswith("index-p"):
            continue
        if _strategy(_parts[0]) != "flat":
            _seats[(_parts[0], _stem)].append(rel)
    for (_ns, _slug), _paths in sorted(_seats.items()):
        if len(_paths) > 1:
            continue        # a DUPLICATE — deployment_checks.check_partition_dups() owns that
        if _strategy(_ns) not in _SLUG_ONLY_STRATEGIES:
            misfiled_unchecked.add(f"{_ns} ({_strategy(_ns)})")
            continue
        if _slug.startswith("_"):
            continue                               # reserved/meta page (`_about`) — never sharded
        try:
            _canon = okf_migrate.canonical_key(vault, _ns, _slug)
        except Exception:
            continue
        if not _canon or _canon == _paths[0]:
            continue
        # canonical_key returns the BASE seat. A namespace over `reshard_over` is legitimately
        # refined deeper by `reshard_by` (`concepts/a/AI-x` -> `concepts/a/i/AI-x`), so a deeper
        # path under the same seat is CORRECT, not misfiled. Without this the check called all
        # 13,214 resharded pages defects -- a number confidently wrong enough to bury the 74 real
        # collisions it was built to surface.
        _canon_dir, _actual_dir = _canon.rsplit("/", 1)[0], _paths[0].rsplit("/", 1)[0]
        if _actual_dir.startswith(_canon_dir + "/"):
            continue                               # a reshard refinement of the canonical seat
        partition_misfiled.append(f"{_paths[0]} → canonical seat {_canon}")

    dangling: dict = defaultdict(lambda: {"count": 0, "examples": []})
    for field, target, src in path_refs:
        if target.split("/", 1)[0] not in namespaces:
            continue                                   # not a wiki page namespace
        if target in existing_paths or target in existing_dirs:
            continue                                   # resolves to a page or a shard/dir
        rec = dangling[field]
        rec["count"] += 1
        if len(rec["examples"]) < MAX_EXAMPLES:
            rec["examples"].append(f"{src} → {target}")

    return {
        "pages": pages,
        "parse_errors": parse_errors,
        "dangling_refs": {f: dict(r) for f, r in dangling.items()},
        "off_taxonomy": {t: dict(rec) for t, rec in off_taxonomy.items()},
        "fragmentation": fragmentation,
        "drift": {f: dict(vals) for f, vals in drift.items()},
        "novel": {f: dict(vals) for f, vals in novel.items()},
        "populated": populated,
        "candidates": candidates,
        "enums_declared": enums_declared,
        "coverage_declared": coverage_declared,
        "coverage": {f"{t}.{f}": {"total": rec["total"], "have": rec["have"], "min": rec["min"],
                                  "ratio": (rec["have"] / rec["total"] if rec["total"] else 0.0)}
                     for (t, f), rec in sorted(coverage.items())},
        "prediction_loop": prediction_loop,
        # okengine#54, the PRE-duplication half. Duplicates are deployment_checks'
        # check_partition_dups(); this is the single-copy-in-the-wrong-place signal it cannot see.
        # A raw capture is a source record wherever it sits, so the `sources` namespace governs
        # its vocabulary. Resolved separately from the per-page cache above because raw/ lives
        # OUTSIDE wiki/ and so never appears in that walk. None (not {}) when unresolvable, so
        # the report says UNDETECTABLE rather than reporting a clean it never measured.
        "raw_capture_health": raw_capture_health(vault, _sources_enum_rules(vault)),
        "partition_misfiled": partition_misfiled[:MAX_EXAMPLES],
        "partition_misfiled_count": len(partition_misfiled),
        "partition_misfiled_unchecked": sorted(misfiled_unchecked),
        "source_partition_collisions": [
            sorted(paths, key=lambda path: (len(Path(path).parts), path))
            for paths in source_signatures.values()
            if len(paths) > 1
            and any(len(Path(path).parts) == 4 for path in paths)
            and any(len(Path(path).parts) == 5 for path in paths)
        ],
        "review_queue": {
            "total": review_total,
            "substantive": review_substantive,
            "fraction": (review_total / pages) if pages else 0.0,
            "median_age_days": median(review_ages) if review_ages else None,
        },
        "malformed_slugs": {
            "count": malformed_slug_count,
            "examples": malformed_slugs,
        },
        # Two pages for one subject, each correctly filed -- so every EQUALITY-based partition
        # check passes and the pair survives indefinitely. Downstream, both get counted, ranked
        # and worked separately: one live actor-assessment-priority duplicate on this corpus
        # traced straight back to `cyber-army-of-russia-reborn` also existing as
        # `cyberarmy-of-russia-reborn`.
        "near_duplicate_slugs": near_duplicate_slugs[:MAX_EXAMPLES],
        "near_duplicate_slug_count": len(near_duplicate_slugs),
        "body_integrity": body_integrity,
    }
