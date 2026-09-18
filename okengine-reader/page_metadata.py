"""Pure presentation helpers for reader page metadata."""
from __future__ import annotations

import re
import json
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.parse import urlsplit, urlunsplit


def compact_dict(value: dict) -> str:
    return ", ".join(
        f"{key}={item}" for key, item in value.items() if item not in (None, "", [], {})
    )


def value_text(value) -> str:
    return compact_dict(value) if isinstance(value, dict) else str(value)


def shape_conflicts(frontmatter: dict, *, source_reliability, reliability_rank) -> list[dict]:
    """Shape conflicting claims into reliability-ranked display records."""
    reliability = source_reliability()
    output: list[dict] = []
    conflicts = frontmatter.get("conflicts")
    if not isinstance(conflicts, list):
        conflicts = []
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            continue
        headline = conflict.get("headline")
        values: list[dict] = []
        raw_values = conflict.get("values")
        if not isinstance(raw_values, list):
            raw_values = []
        for raw_value in raw_values:
            if not isinstance(raw_value, dict):
                continue
            raw_sources = raw_value.get("sources")
            if not isinstance(raw_sources, list):
                raw_sources = []
            sources = []
            for source in raw_sources:
                grade = reliability.get(str(source), "")
                normalized = str(grade).strip().upper()
                rank_key = normalized
                # Preserve the legacy combined Admiralty spelling (for example B2): the leading
                # source-reliability letter is still rankable even when credibility is appended.
                if normalized not in reliability_rank and re.fullmatch(r"[A-F][1-6]", normalized):
                    rank_key = normalized[0]
                recognized = None if not normalized else rank_key in reliability_rank
                sources.append(
                    {
                        "name": str(source),
                        "reliability": grade,
                        "reliability_recognized": recognized,
                        "reliability_rank_key": rank_key,
                    }
                )
            rank = max(
                (
                    reliability_rank[source["reliability_rank_key"]]
                    for source in sources
                    if source["reliability_recognized"] is True
                ),
                default=-1,
            )
            values.append(
                {
                    "value": value_text(raw_value.get("value")),
                    "sources": sources,
                    "rank": rank,
                    "rank_known": any(
                        source["reliability_recognized"] is True for source in sources
                    ),
                    "reliability_oov": any(
                        source["reliability_recognized"] is False for source in sources
                    ),
                    "is_headline": raw_value.get("value") == headline,
                }
            )
        output.append(
            {
                "field": str(conflict.get("field") or ""),
                "headline": value_text(headline),
                "values": values,
            }
        )
    return output


def url_label(url: str) -> str:
    """Return a concise host label for an external URL."""
    try:
        host = urlparse(url).netloc
    except Exception:
        host = ""
    host = host[4:] if host.startswith("www.") else host
    return host or url


def ref_target(value: str, *, wiki: Path, skip, within) -> str | None:
    """Resolve a path-shaped metadata value to its canonical wiki key."""
    if not isinstance(value, str):
        return None
    key = value.strip()
    if "/" not in key or "://" in key or " " in key:
        return None
    key = key[:-3] if key.endswith(".md") else key
    if not wiki.is_dir():
        return None
    try:
        candidate = (wiki / (key + ".md")).resolve()
        if candidate.is_file() and within(wiki, candidate):
            return key
        hits = [path for path in wiki.rglob(Path(key).name + ".md") if not skip(path.name)]
        if len(hits) == 1:
            return str(hits[0].resolve().relative_to(wiki.resolve()))[:-3]
    except OSError:
        pass
    return None


def meta_values(value, *, wiki: Path, skip, within) -> list[dict]:
    """Convert a frontmatter value into display chips and resolved links."""
    output: list[dict] = []
    for element in value if isinstance(value, list) else [value]:
        if isinstance(element, dict):
            url = element.get("url") or element.get("href")
            text = (
                element.get("id")
                or element.get("value")
                or element.get("name")
                or element.get("std")
                or (url_label(url) if url else None)
                or compact_dict(element)
            )
            output.append({"text": str(text), "url": str(url)} if url else {"text": str(text)})
        else:
            text = str(element)
            if text.startswith(("http://", "https://")):
                output.append({"text": url_label(text), "url": text})
            else:
                target = ref_target(text, wiki=wiki, skip=skip, within=within)
                output.append({"text": text, "page": target} if target else {"text": text})
    return output


def panel_items(
    frontmatter: dict, *, wiki: Path, skip, within, panel_skip: set, secondary_keys: set
) -> dict:
    """Split frontmatter into primary and secondary display rows."""
    primary: list[dict] = []
    secondary: list[dict] = []
    if not isinstance(frontmatter, dict):
        return {"primary": primary, "secondary": secondary}
    for key, value in frontmatter.items():
        if key in panel_skip or value is None or value == "" or value == [] or value == {}:
            continue
        label = str(key).replace("_", " ").replace("-", " ").strip()
        item = {
            "label": label[:1].upper() + label[1:],
            "values": meta_values(value, wiki=wiki, skip=skip, within=within),
        }
        (secondary if key in secondary_keys else primary).append(item)
    return {"primary": primary, "secondary": secondary}


def recent_reporting(frontmatter: dict, *, wiki: Path, split_frontmatter) -> list[dict]:
    """Group recent source references by explicit or canonical-URL story identity."""
    refs = frontmatter.get("recent_news_refs") or []
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, list):
        return []
    groups: dict[str, dict] = {}
    for raw in refs:
        relative = str(raw).strip().strip("[] ")
        if relative.endswith(".md"):
            relative = relative[:-3]
        if not relative.startswith("sources/"):
            continue
        try:
            path = (wiki / f"{relative}.md").resolve()
            path.relative_to(wiki.resolve())
            source, _ = split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        explicit = (
            source.get("story_id")
            or source.get("same_story_cluster")
            or source.get("cluster_id")
        )
        url = str(source.get("canonical_url") or source.get("url") or "").strip()
        if explicit:
            key = f"cluster:{explicit}"
        elif url:
            try:
                parts = urlsplit(url)
                normalized = urlunsplit(
                    ("", parts.netloc.lower().removeprefix("www."), parts.path.rstrip("/"), "", "")
                )
                key = "url:" + normalized
            except ValueError:
                key = f"path:{relative}"
        else:
            key = f"path:{relative}"
        group = groups.setdefault(
            key,
            {
                "title": str(source.get("title") or source.get("name") or Path(relative).name),
                "sources": [],
            },
        )
        group["sources"].append(
            {"path": relative, "publisher": str(source.get("publisher") or "")}
        )
    return [{**group, "count": len(group["sources"])} for group in groups.values()]


def entity_assessments(
    frontmatter: dict, relative: str, *, wiki: Path, split_frontmatter
) -> list[dict]:
    """Return active assessment records explicitly bound to an entity."""
    base = wiki / "assessments"
    if not base.is_dir():
        return []
    subject_id = str(frontmatter.get("id") or "").strip()
    normalized_relative = relative.removeprefix("wiki/").removesuffix(".md")
    rows = []
    for path in base.rglob("*.md"):
        try:
            assessment, _ = split_frontmatter(
                path.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            continue
        subject = (
            str(assessment.get("subject") or "")
            .strip()
            .removeprefix("wiki/")
            .removesuffix(".md")
        )
        subject_ref = str(assessment.get("subject_ref") or "").strip()
        if assessment.get("status", "active") != "active" or not (
            subject == normalized_relative or (subject_id and subject_ref == subject_id)
        ):
            continue
        assessment_relative = str(path.relative_to(wiki))[:-3]
        rows.append(
            {
                "path": assessment_relative,
                "title": str(assessment.get("title") or path.stem),
                "kind": str(assessment.get("assessment_kind") or "assessment"),
                "claim": str(assessment.get("claim") or ""),
                "assessed_value": assessment.get("assessed_label")
                or assessment.get("assessed_value"),
                "confidence": assessment.get("confidence"),
                "confidence_band": assessment.get("confidence_band"),
                "epistemic_status": assessment.get("epistemic_status"),
                "needs_review": bool(assessment.get("needs_review")),
                "last_updated": assessment.get("last_updated") or assessment.get("as_of"),
            }
        )
    return sorted(rows, key=lambda row: str(row["last_updated"] or ""), reverse=True)


def provenance(frontmatter: dict, body: str, *, source_reliability) -> dict:
    """Build the page grounding and human-review trust summary."""
    sources = frontmatter.get("sources")
    sources = sources if isinstance(sources, list) else ([sources] if sources else [])
    page_sources = sum(
        1 for source in sources if "/" in str(source) or str(source).lower().endswith(".md")
    )
    registry = source_reliability()
    graded_sources = (
        sum(1 for source in sources if registry.get(str(source).strip())) if registry else 0
    )
    grounding = None
    match = re.search(r"##\s+Grounding check(.*?)(?:\n##\s|\Z)", body, re.S | re.I)
    if match:
        segment = match.group(1)
        grounding = {
            "supported": len(re.findall(r"\*\*\s*supported", segment, re.I)),
            "unsupported": len(
                re.findall(r"\*\*\s*(?:unsupported|not[- ]found|contradict)", segment, re.I)
            ),
        }
    return {
        "sources": len(sources),
        "source_pages": page_sources,
        "graded_sources": graded_sources,
        "registry_available": bool(registry),
        "reviewed_by": frontmatter.get("reviewed_by"),
        "reviewed_on": frontmatter.get("reviewed_on"),
        "needs_review": bool(frontmatter.get("needs_review")),
        "grounding": grounding,
    }


def source_reliability(*, cache: tuple, ttl: float, schema_path, yaml_module) -> tuple[dict, tuple]:
    now = time.monotonic()
    if now - cache[0] < ttl:
        return cache[1], cache
    output: dict = {}
    path = schema_path()
    if path.is_file():
        try:
            registry = (
                yaml_module.safe_load(path.read_text(encoding="utf-8")) or {}
            ).get("source_registry") or {}
            for key, value in registry.items() if isinstance(registry, dict) else []:
                reliability = str((value or {}).get("reliability") or "").strip()
                if reliability:
                    output[str(key)] = reliability
        except Exception:
            pass
    updated = (now, output)
    return output, updated
def observations_by_canonical(
    *, cache: tuple[float, dict], ttl: float, wiki: Path, skip, reserved,
    split_frontmatter, read_head,
) -> tuple[dict, tuple[float, dict]]:
    """Index live observations by canonical slug, retaining the caller-owned cache."""
    now = time.monotonic()
    if now - cache[0] < ttl:
        return cache[1], cache
    output: dict = {}
    base = wiki / "observations"
    if base.is_dir():
        for path in base.rglob("*.md"):
            if skip(path.name) or reserved(path):
                continue
            frontmatter, _ = split_frontmatter(read_head(path))
            canonical = str(frontmatter.get("canonical") or "").strip().lower()
            if canonical:
                key = str(path.resolve().relative_to(wiki.resolve()))[:-3]
                output.setdefault(canonical, []).append(
                    {"source": str(frontmatter.get("source") or ""), "key": key}
                )
    return output, (now, output)


def page_response(
    path: str, *, resolve_page, split_frontmatter, metadata_items, render,
    panel_for, provenance_for, conflicts_for, observations, assessments_for,
    recent_reporting_for, wiki: Path,
) -> dict:
    """Assemble the public page representation from domain collaborators."""
    page = resolve_page(path)
    frontmatter, body = split_frontmatter(page.read_text(encoding="utf-8", errors="replace"))
    title = frontmatter.get("title") or frontmatter.get("name") or Path(path).name
    metadata = metadata_items(frontmatter)
    relative = str(page.relative_to(wiki.resolve()))[:-3]
    return {
        "path": path, "title": str(title), "type": str(frontmatter.get("type") or ""),
        "rel": relative, "html": render(body), "meta": metadata["primary"],
        "meta_aux": metadata["secondary"], "panel": panel_for(frontmatter, body),
        "provenance": provenance_for(frontmatter, body),
        "conflicts": conflicts_for(frontmatter),
        "needs_review": bool(frontmatter.get("needs_review")),
        "observations": observations().get(page.stem.lower(), []),
        "assessments": assessments_for(frontmatter, relative),
        "recent_reporting": recent_reporting_for(frontmatter),
    }
def reader_panels(*, cache: list, vault: Path, now=None) -> dict:
    """Load the staged type-to-panel registry through its short deployment cache."""
    current = time.time() if now is None else now
    if cache[1] is None or current - cache[0] > 60:
        try:
            cache[1] = json.loads((vault / ".okengine" / "reader-panels.json").read_text())
        except Exception:
            cache[1] = {}
        cache[0] = current
    return cache[1] or {}
