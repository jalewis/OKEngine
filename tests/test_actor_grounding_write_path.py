"""The write path grounds a new actor in its cited sources (okengine#1012).

The entity-backfill lane minted a sandbox vendor, a hosting provider, a package registry, a
campaign name and a nation-nexus adjective as `type: actor`, through the enforced write path. Every
check it passed read the profile the model had just written, so "ANY.RUN is a threat group ..."
attested to itself. These tests replay the lane's real payloads (read from a live CTI vault's
session records) against the real source text they cited, trimmed to the cited passage. Each is
refused on this branch; on the parent commit every one of them is accepted.

The replays run the entity-backfill tool's two steps in order: `_entity_backfill_frontmatter`
(the lane's pre-check), then `_converge`.
"""
from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WS_MOD = REPO / "okengine-mcp" / "write_server.py"

_SCHEMA = (
    "okf:\n  required: [type]\ntypes:\n  actor:\n    required: [type]\n  identity:\n"
    "    required: [type]\n  publisher:\n    required: [type]\n  malware:\n    required: [type]\n"
    "  source:\n    required: [type]\nstrict_types: false\n"
    "type_aliases: {threat-actor: actor}\n"
)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "entities").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", datetime.date(2026, 10, 8).isoformat())
    sys.modules.pop("write_server", None)
    spec = importlib.util.spec_from_file_location("write_server", WS_MOD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = module
    spec.loader.exec_module(module)
    return module, tmp_path


def _source(root: Path, ref: str, title: str, body: str, **fields) -> str:
    page = root / "wiki" / f"{ref}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    fm = {"type": "source", "title": title, **fields}
    page.write_text(f"---\n{yaml.safe_dump(fm, sort_keys=False)}---\n\n# {title}\n\n{body}\n",
                    encoding="utf-8")
    return ref


def _page(root: Path, rel: str) -> Path | None:
    found = list((root / "wiki").glob(f"{rel}.md")) + list((root / "wiki").rglob(
        f"{rel.rsplit('/', 1)[-1]}.md"))
    return found[0] if found else None


def _backfill(m, path: str, frontmatter: str, body: str) -> str:
    sanitized, error = m._entity_backfill_frontmatter(path, frontmatter, body)
    return error or m._converge(path, sanitized or "", body)


# --- the real payloads ---------------------------------------------------------------------------
# path, frontmatter_yaml and body are the lane's own tool arguments (its accepted attempt for each
# subject); the source is the canonical page it cited, title verbatim, body trimmed to the passage
# that mentions the subject (or, where the subject is never named, the article's opening).

_WAZZA = ("sources/2026/10/08/2026-10-08-wazza-phishkit-targets-banking-government-and-"
          "manufacturing-across-the-us-eu-and-e8a1c9de")
_OMEGA = ("sources/2026/10/05/nearly-40-000-phishing-attacks-hit-us-financial-firms-in-h1-2026-"
          "automated-ai-ag-0a4977fa")
_BLINDER = "sources/2026/10/06/blinder-tunnel-campaign-targets-iraqi-infrastructure-7669d348"
_POELLM = ("sources/2026/10/07/2026-10-07-poellm-malware-infects-3-400-servers-to-expand-crypto-"
           "mining-botnet-99a195ea")
_FIREFOX = ("sources/2026/10/08/2026-10-08-16-malicious-firefox-extensions-pose-as-rabby-and-okx-"
            "wallets-to-steal-recovery--0a0a43d5")

_SOURCES = {
    _WAZZA: ("Wazza Phishkit Targets Banking, Government, and Manufacturing Across the US,",
             "Phishing kits are no longer limited to copying a familiar login page and waiting for a "
             "victim to enter credentials. Attackers are increasingly building filtering, session "
             "management, and traffic controls into the infrastructure that delivers the phishing "
             "page itself. ANY.RUN has identified Wazza, a new phishkit targeting banking, "
             "manufacturing, and government organizations across the US, Europe",
             "The Hacker News"),
    _OMEGA: ("Nearly 40,000 phishing attacks hit US financial firms in H1 2026",
             "Omegatech emerges as another source of attack infrastructure The infrastructure "
             "picture changed further with the emergence of Omegatech, a paid hosting provider "
             "based in the Seychelles, which started operations in January 2026. By June, Netcraft "
             "attributed roughly 3% of observed phishing attacks against US financial services to "
             "infrastructure hosted through Omegatech.",
             "techradar.com"),
    _BLINDER: ("Blinder Tunnel Campaign Targets Iraqi Infrastructure",
               "Analysis of Blinder Tunnel, an Iran-nexus campaign using fake Dubai Airports "
               "recruitment lures and GitHub C2 malware to target critical infrastructure. The post "
               "Blinder Tunnel Campaign Targets Iraqi Infrastructure appeared first on Unit 42.",
               "unit42.paloaltonetworks.com"),
    _POELLM: ("PoeLLM Malware Infects 3,400+ Servers to Expand Crypto Mining Botnet",
              "Cybersecurity researchers are calling attention to a new malware family that has "
              "been observed targeting exposed artificial intelligence (AI) and large language "
              "model (LLM) infrastructure with an aim to deploy cryptocurrency miners and further "
              "expand the scale of the botnet. The financially motivated campaign, dubbed Canto "
              "Incognito, has been found to install cryptocurrency miners, including",
              "The Hacker News"),
    _FIREFOX: ("16 Malicious Firefox Extensions Pose as Rabby and OKX Wallets to Steal Recovery",
               "Cybersecurity researchers have discovered a cluster of 16 malicious Mozilla Firefox "
               "extensions that are capable of stealing cryptocurrency wallet recovery phrases and "
               "private keys.",
               "The Hacker News"),
}

_PAYLOADS = {
    "ANY.RUN (a sandbox vendor that REPORTED the phishkit)": (
        "entities/a/anyrun",
        f"type: actor\nactor_type: cybercriminal\nid: anyrun\ntitle: ANY.RUN\nsources:\n- {_WAZZA}\n",
        "ANY.RUN is a threat group identified as the operator of the Wazza phishkit targeting "
        "banking, manufacturing, and government organizations across the US, Europe, and "
        "Australia."),
    "Omegatech (a hosting provider)": (
        "entities/a/agent-omega",
        "actor_type: cybercriminal\nalias:\n- Omegatech\n- Omegatech Inc\n- Omegatech Hosting\n"
        f"confidence: 0.8\nidentity: Omegatech\nid: agent-omega\nname: Omegatech\nsources:\n"
        f"- {_OMEGA}.md\ntype: actor\n",
        "Omegatech is a threat actor group that has been identified as a hosting provider based "
        "in the Seychelles. They have been involved in phishing campaigns targeting financial "
        "services in the United States, with their infrastructure being used to impersonate 41 "
        "financial brands. This group is known for providing bulletproof hosting services that "
        "allow attackers to operate with minimal risk of detection or takedown."),
    "Iran-nexus (an attribution adjective)": (
        "entities/i/iran-nexus",
        f"actor_type: nation-state\nid: iran-nexus\nname: Iran-nexus\ntype: actor\nsources:\n"
        f"- {_BLINDER}.md\n",
        "Iran-nexus is a nation-state intrusion set associated with the Blinder Tunnel campaign, "
        "targeting critical infrastructure in Iraq using fake Dubai Airports recruitment lures and "
        "GitHub C2 malware."),
    "Canto Incognito (a campaign name)": (
        "entities/c/canto-incognito",
        "id: canto-incognito\ntype: actor\nactor_type: cybercriminal\ntitle: Canto Incognito\n"
        f"sources:\n- {_POELLM}.md\n",
        "Canto Incognito is a financially motivated intrusion set that targets exposed AI and LLM "
        "infrastructure to deploy cryptocurrency miners."),
    "Anonymous Dog (named nowhere in its source)": (
        "entities/a/anonymous-dog",
        "actor_type: hacktivist\nalias:\n- Anonymous Dog\nconfidence: high\nid: anonymous-dog\n"
        f"name: Anonymous Dog\nsources:\n- {_FIREFOX}\nstatus: active\ntype: actor\n",
        "Anonymous Dog is an intrusion set that has been associated with the deployment of "
        "malicious Firefox extensions designed to steal cryptocurrency wallet recovery phrases and "
        "private keys."),
}


def _seed_sources(root: Path) -> None:
    for ref, (title, body, publisher) in _SOURCES.items():
        _source(root, ref, title, body, publisher=publisher, source_kind="report")


@pytest.mark.parametrize("subject", sorted(_PAYLOADS))
def test_a_real_entity_backfill_payload_is_refused_when_no_source_affirms_the_actor(ws, subject):
    """Red on the parent commit: each payload was created there, `has_positive_actor_identity`
    having read only the model's own sentence."""
    m, root = ws
    _seed_sources(root)
    path, frontmatter, body = _PAYLOADS[subject]
    out = _backfill(m, path, frontmatter, body)
    assert out.startswith("rejected: creating an actor needs a cited source"), out
    assert _page(root, path) is None, f"{subject}: nothing may be written"


def test_the_refusal_names_what_the_source_says_the_subject_is(ws):
    m, root = ws
    _seed_sources(root)
    expected = {
        "ANY.RUN (a sandbox vendor that REPORTED the phishkit)": "reporting organization",
        "Omegatech (a hosting provider)": "hosting provider",
        "Iran-nexus (an attribution adjective)": "campaign",
        "Canto Incognito (a campaign name)": "campaign",
        "Anonymous Dog (named nowhere in its source)": "name_absent_from_sources",
    }
    for subject, reason in expected.items():
        out = _backfill(m, *_PAYLOADS[subject])
        assert reason in out, (subject, out)


# --- npm: an identity page converged into an actor (2026-08-08) ----------------------------------

_NPM_IDENTITY_SOURCE = ("sources/2026/08/05/2026-08-05-trojanized-npm-packages-decode-c2-ip-from-"
                        "ethereum-recipient-addresses-66d9046c")
_NPM_WIZ_REF = "sources/private/curated/wiz/12bafdea090146061d0b"
_NPM_WIZ_ID = "source:curated:wiz:12bafdea090146061d0b"


def _npm_identity(m, root: Path) -> Path:
    """The page as the same lane created it on 2026-08-05: an `identity`, the ecosystem name in
    `publisher`. Then the curated campaign record the 2026-08-08 payload cited, whose upstream
    actor is "Unknown"."""
    _source(root, _NPM_IDENTITY_SOURCE,
            "Trojanized npm Packages Decode C2 IP from Ethereum Recipient Addresses",
            "Cybersecurity researchers have flagged an evolution of the EtherHiding "
            "blockchain-based command-and-control (C2) technique. The new dead drop resolver "
            "approach, observed in two trojanized npm packages, has been codenamed NullReceiver.",
            publisher="The Hacker News", source_kind="report")
    _source(root, _NPM_WIZ_REF,
            "NPM Supply Chain Attack Compromises 16 Popular React Native and GlueStack Packages "
            "— Wiz cloud campaign",
            "A threat actor compromised 16 highly popular React Native and GlueStack packages, "
            "collectively downloaded over a million times weekly.\n\n## Attribution\n\n"
            "- Reported actor: Unknown\n",
            id=_NPM_WIZ_ID, publisher="Wiz", source_kind="reference-data",
            evidence_role="curated-authority", stix_type="campaign")
    created = m._converge(
        "entities/n/npm",
        f"id: npm\ntype: identity\npublisher: npm\ntitle: npm\nsources:\n- {_NPM_IDENTITY_SOURCE}\n",
        "Trojanized npm packages decode a C2 address from Ethereum transfers.")
    assert created.startswith("created"), created
    m._registry().by_id[_NPM_WIZ_ID] = f"{_NPM_WIZ_REF}.md"
    page = _page(root, "entities/n/p/npm")
    assert page is not None
    return page


def test_the_npm_identity_page_cannot_be_converged_into_an_actor(ws):
    """Session cron_9f869c8f7181_20260808_141536, verbatim. The lane owned the page, so the merge
    took its `type: actor` with no conflict and no review flag: "+2 added, ~2 updated, 0
    conflicts". Red on the parent commit."""
    m, root = ws
    page = _npm_identity(m, root)
    before = page.read_bytes()
    out = m._converge(
        "entities/n/npm",
        f"id: actor:npm\ntype: actor\nactor_type: unknown\nstatus: active\nsources:\n"
        f"- {_NPM_WIZ_ID}\n",
        "## Recent activity\n\n- 2025: Compromised 16 highly popular React Native and GlueStack "
        "packages, collectively downloaded over a million times weekly.")
    assert out.startswith("rejected: retyping this 'identity' page to actor"), out
    assert "name_equals_publisher" in out, out
    assert page.read_bytes() == before


def test_a_self_attesting_npm_payload_cannot_retype_it_through_the_lane_either(ws):
    """The lane's pre-check refuses the nameless 2026-08-08 payload today, but one added sentence
    and a title would carry it: has_positive_actor_identity reads the model's own body. Red on the
    parent commit."""
    m, root = ws
    page = _npm_identity(m, root)
    before = page.read_bytes()
    out = _backfill(
        m, "entities/n/npm",
        f"id: actor:npm\ntype: actor\nactor_type: unknown\ntitle: npm\nsources:\n- {_NPM_WIZ_ID}\n",
        "npm is a threat actor group that compromised 16 React Native and GlueStack packages.")
    assert out.startswith("rejected: retyping this 'identity' page to actor"), out
    assert page.read_bytes() == before


@pytest.mark.parametrize("operation", ["update", "patch"])
def test_a_retype_to_actor_is_judged_on_update_and_patch(ws, operation):
    m, root = ws
    page = _npm_identity(m, root)
    before = page.read_bytes()
    if operation == "update":
        out = m._update("entities/n/p/npm", {"type": "actor", "actor_type": "unknown"})
    else:
        out = m._patch("entities/n/p/npm", "type: identity", "type: actor")
    assert out.startswith("rejected: retyping this 'identity' page to actor"), out
    assert page.read_bytes() == before


def test_a_retype_the_sources_ground_is_accepted(ws):
    """Not a ban on retyping: an identity page whose cited source affirms the adversary role may
    become an actor."""
    m, root = ws
    ref = _source(root, "sources/2026/09/01/crew-report", "Crew report",
                  "Night Crew claimed the intrusion at a regional bank.", source_kind="report")
    created = m._create("entities/n/night-crew",
                        {"type": "identity", "title": "Night Crew", "sources": [ref]}, "Profile.")
    assert created.startswith("created"), created
    out = m._update("entities/n/night-crew", {"type": "actor", "actor_type": "cybercriminal"})
    assert out.startswith("updated"), out


# --- what must still pass -------------------------------------------------------------------------

_AFFIRMED = {
    "LockBit": ("US Bank Examines Alleged Data Breach After LockBit Ransomware Extortion Claim",
                "US Bank is investigating claims by the LockBit ransomware operation that it "
                "breached the financial institution and stole undisclosed data. LockBit claimed "
                "the attack on its leak site."),
    "Scattered Spider": ("Retail intrusions", "Scattered Spider targeted retail help desks with "
                         "voice phishing calls."),
    # The engine's known actor_identity_error false positives: each is affirmed by its own source.
    "Red Heron": ("Red Heron Exploits Critical Gitea Flaw to Steal Repositories and Deploy Linux "
                  "Rootkit", "A threat actor tracked as Red Heron has exploited the critical Gitea "
                  "remote code execution vulnerability."),
    "Smishing Triad": ("Smishing Triad Hackers Use JWR Phishing Kit to Steal Cards, OTPs and Bank "
                       "Credentials", "A large-scale SMS phishing campaign linked to the Smishing "
                       "Triad is using a sophisticated phishing kit dubbed JWR."),
    "TA488": ("AI-speed attacks and critical flaws compress defenders' response window",
              "Russia-aligned TA488 is exploiting an Outlook Web Access half-click vulnerability "
              "that executes malicious JavaScript when a crafted email is opened."),
    "JadePuffer": ("JadePuffer agentic AI attacks target Azure, destroy cloud resources",
                   "The JadePuffer ransomware operator is targeting Azure tenants with "
                   "agent-driven attacks that conduct reconnaissance and steal credentials."),
}


@pytest.mark.parametrize("name", sorted(_AFFIRMED))
def test_an_actor_its_source_affirms_is_created(ws, name):
    m, root = ws
    slug = name.lower().replace(" ", "-")
    title, body = _AFFIRMED[name]
    ref = _source(root, f"sources/2026/09/20/{slug}-report", title, body, source_kind="news")
    out = _backfill(m, f"entities/{slug[0]}/{slug}",
                    f"type: actor\nactor_type: cybercriminal\ntitle: {name}\nsources:\n- {ref}\n",
                    f"{name} is a threat group that targets organizations.")
    assert out.startswith("created"), (name, out)


def test_a_curated_actor_record_vouches_without_narrative(ws):
    """An attested curated record OF the actor vouches for it; it has no prose to affirm with."""
    m, root = ws
    ref = _source(root, "sources/private/curated/actor/2af912cb5cf966a3ad25",
                  "Worok — CERT actor record", "No description supplied.",
                  source_kind="reference-data", evidence_role="curated-authority")
    out = m._create("entities/w/worok", {"type": "actor", "title": "Worok", "sources": [ref]}, "")
    assert out.startswith("created"), out


def test_a_curated_record_of_something_else_does_not_vouch(ws):
    """Curated datasets also catalog malware, campaigns and ransomware VICTIMS. A record that is
    not an actor record vouches for nothing."""
    m, root = ws
    ref = _source(root, "sources/private/curated/tool-family/f3972a1febaf5155300a",
                  "CARROTBALL — catalog record",
                  "CARROTBALL is a simple FTP downloader built to deploy SYSCON.",
                  source_kind="reference-data", evidence_role="curated-authority")
    out = m._create("entities/c/carrotball", {"type": "actor", "title": "CARROTBALL",
                                              "sources": [ref]}, "")
    assert out.startswith("rejected: creating an actor"), out


def test_a_provenance_label_is_not_a_source_for_a_model_write(ws):
    """A model can type any label; only a deterministic importer of that list may rely on it."""
    m, root = ws
    out = m._create("entities/a/apt-zero", {"type": "actor", "title": "APT Zero",
                                            "sources": ["MITRE ATT&CK"]}, "APT Zero is a group.")
    assert out.startswith("rejected: creating an actor") and "no_resolvable_source" in out, out


@pytest.mark.parametrize("operation", ["update", "converge", "patch"])
def test_an_existing_actor_stays_editable_without_new_evidence(ws, operation):
    """Only a create or a retype is judged: enrichment of a stored actor is not re-litigated."""
    m, root = ws
    page = root / "wiki" / "entities" / "a" / "apt28.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntype: actor\nid: G0007\ntitle: APT28\nsources:\n- MITRE ATT&CK\n---\n\n"
                    "Profile.\n", encoding="utf-8")
    if operation == "update":
        out = m._update("entities/a/apt28", {"recent_news": 3})
    elif operation == "converge":
        out = m._converge("entities/a/apt28", "type: actor\nid: G0007\ntitle: APT28\nrecent_news: 3\n",
                          "")
    else:
        out = m._patch("entities/a/apt28", "Profile.", "Profile, enriched.")
    assert out.startswith(("updated", "converged", "patched")), out


def test_a_create_that_dedups_onto_an_existing_actor_is_a_merge_not_a_create(ws):
    """Grounding runs after dedup routing: a create resolving onto a stored actor merges into it."""
    m, root = ws
    page = root / "wiki" / "entities" / "a" / "apt28.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntype: actor\nid: G0007\ntitle: APT28\naliases: [Fancy Bear]\nsources:\n"
                    "- MITRE ATT&CK\n---\n\nProfile.\n", encoding="utf-8")
    out = m._create("entities/f/fancy-bear", {"type": "actor", "title": "Fancy Bear",
                                              "aliases": ["APT28"]}, "")
    assert "creating an actor" not in out, out


def test_an_id_reference_the_index_cannot_resolve_is_not_evidence(ws, monkeypatch):
    """Fail closed: an id index that cannot be read leaves the reference unresolved."""
    m, _root = ws

    def unreadable_index():
        raise OSError("id index unreadable")

    monkeypatch.setattr(m, "_registry", unreadable_index)
    assert m._actor_source_page("source:curated:x") is None


def test_a_cited_page_outside_sources_is_not_evidence(ws):
    m, root = ws
    other = root / "wiki" / "entities" / "n" / "notes.md"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("---\ntype: identity\n---\n\nNight Crew claimed the intrusion.\n",
                     encoding="utf-8")
    out = m._create("entities/n/night-crew", {"type": "actor", "title": "Night Crew",
                                              "sources": ["entities/n/notes"]}, "")
    assert out.startswith("rejected: creating an actor") and "no_resolvable_source" in out, out


def test_an_id_reference_resolves_through_the_id_index(ws):
    """`sources:url-...` ids are how converge lanes cite: the id index resolves them."""
    m, root = ws
    ref = _source(root, "sources/2026/09/02/crew-claim", "Crew claim",
                  "Night Crew claimed the intrusion at a regional bank.", id="sources:url-crew")
    m._registry().by_id["sources:url-crew"] = f"{ref}.md"
    out = m._create("entities/n/night-crew", {"type": "actor", "title": "Night Crew",
                                              "sources": ["sources:url-crew"]}, "")
    assert out.startswith("created"), out


def test_a_cited_page_in_a_namespace_sorting_after_sources_is_not_evidence(ws):
    m, root = ws
    other = root / "wiki" / "topics" / "notes.md"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("---\ntype: topic\n---\n\nNight Crew claimed the intrusion.\n",
                     encoding="utf-8")
    assert m._actor_source_page("topics/notes") is None


@pytest.mark.parametrize("error", [OSError("unreadable"), UnicodeDecodeError(
    "utf-8", b"\xff", 0, 1, "invalid start byte")])
def test_an_unreadable_cited_source_is_unresolved(ws, monkeypatch, error):
    m, root = ws
    ref = _source(root, "sources/2026/09/03/claim", "Claim", "Night Crew claimed the intrusion.")

    def unreadable(_page):
        raise error

    monkeypatch.setattr(m, "_read_page", unreadable)
    documents, _labels, unresolved = m._actor_source_documents({"sources": [ref]})
    assert documents == [] and unresolved == [ref]


def test_only_the_actor_type_is_judged_and_only_an_actor_is_exempt_as_before(ws):
    """Types that sort before "actor" are neither judged as actors nor exempt as stored actors."""
    m, root = ws
    page = root / "wiki" / "entities" / "x.md"
    assert m._actor_grounding_reject(page, {"type": "account", "title": "X"}) is None
    refused = m._actor_grounding_reject(page, {"type": "actor", "title": "X"}, {"type": "account"})
    assert refused and refused.startswith("rejected: retyping this 'account' page to actor")


# --- QA of !1288 ---------------------------------------------------------------------------------

@pytest.mark.parametrize("operation", ["create", "converge"])
def test_b1_a_borrowed_alias_does_not_ground_an_unrelated_subject(ws, operation):
    m, root = ws
    ref = _source(root, "sources/2026/10/08/wazza", "Wazza phishkit",
                  "ANY.RUN has identified Wazza, a new phishkit. The Storm-2477 group deployed it "
                  "against banks.", source_kind="report")
    fm = {"type": "actor", "title": "ANYRUN Labs", "aliases": ["Storm-2477"], "sources": [ref]}
    out = (m._create("entities/a/anyrun-labs", fm, "") if operation == "create"
           else m._converge("entities/a/anyrun-labs", yaml.safe_dump(fm), ""))
    assert out.startswith("rejected: creating an actor"), out


def test_b2_a_victim_followed_by_its_attacker_is_not_the_attacker(ws):
    m, root = ws
    ref = _source(root, "sources/2026/10/08/acme", "Acme Health breach",
                  "A ransomware attack hit Acme Health. Ransomware group Qilin claimed it.",
                  source_kind="news")
    out = _backfill(m, "entities/a/acme-health",
                    f"type: actor\nactor_type: cybercriminal\ntitle: Acme Health\nsources:\n- {ref}\n",
                    "Acme Health is a threat group.")
    assert out.startswith("rejected: creating an actor"), out


def test_b3_a_model_cannot_make_a_news_page_a_curated_record(ws):
    """`source_kind` is writable by a model; only the operator-owned `evidence_role` vouches."""
    m, root = ws
    ref = _source(root, _WAZZA, *_SOURCES[_WAZZA][:2], publisher="The Hacker News",
                  source_kind="report", id="sources:wazza")
    updated = m._update(ref, {"source_kind": "reference-data",
                              "title": "ANY.RUN — threat actor record"})
    assert updated.startswith("updated"), updated
    out = m._create("entities/a/anyrun", {"type": "actor", "title": "ANY.RUN",
                                          "sources": [ref]}, "")
    assert out.startswith("rejected: creating an actor"), out


def test_b3_the_curated_marker_itself_is_operator_owned(ws):
    m, root = ws
    ref = _source(root, _WAZZA, *_SOURCES[_WAZZA][:2], source_kind="report")
    out = m._update(ref, {"evidence_role": "curated-authority"})
    assert out.startswith("refused: evidence_role"), out


@pytest.mark.parametrize("spelling", ["Actor", "ACTOR"])
def test_an_actor_type_in_any_case_is_judged(ws, spelling):
    """A non-strict pack stores the type as written; `Actor` must not skip the check."""
    m, root = ws
    out = m._create("entities/a/anyrun", {"type": spelling, "title": "ANY.RUN"}, "")
    assert out.startswith("rejected: creating an actor") and "unsourced" in out, out
    assert m._actor_grounding_reject(root / "wiki" / "entities" / "x.md",
                                     {"type": "actor", "title": "X"}, {"type": spelling}) is None


def test_b1_the_reporter_cannot_borrow_the_adversary_as_an_alias(ws):
    """QA's backfill repro: the vendor that reported a phishkit is minted with the group that ran
    it as its alias. The source names both, in different sentences, with no alias cue."""
    m, root = ws
    ref = _source(root, "sources/2026/10/08/wazza", "Wazza phishkit",
                  "ANY.RUN has identified Wazza, a new phishkit. The Storm-2477 group deployed it "
                  "against banks.", source_kind="report")
    out = _backfill(m, "entities/a/anyrun",
                    f"type: actor\nactor_type: cybercriminal\ntitle: ANY.RUN\naliases:\n- Storm-2477\n"
                    f"sources:\n- {ref}\n", "ANY.RUN is a threat group.")
    assert out.startswith("rejected: creating an actor"), out
