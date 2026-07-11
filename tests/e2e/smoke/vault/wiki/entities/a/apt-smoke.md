---
type: actor
id: apt-smoke
title: APT Smoke
aliases: [Smoke Bear, Vapor Panda]
origin_country: ZZ
motivation: espionage
techniques: [T1566, T1059]
attribution_confidence: medium
recent_reports: 15
sources:
- Smoke test fixture
---

# APT Smoke

SMOKE_BODY_SENTINEL — this prose body must render FIRST, above the fact panel. APT Smoke is a
fictional adversary used only to exercise the render surfaces. It links to its primary implant
[[entities/m/malware-smoke]] and references a technique inline.

A backtick-wrapped wikilink like `[[entities/m/malware-smoke]]` is DELIBERATELY unwrapped into a
real link (the _uncode_wikilinks contract — generators used to wrap live wikilinks in backticks):
the backticks and the `[[ ]]` must not survive as visible text.

A genuine inline-code span like `LITERAL_CODE_KEPT_x7` must stay LITERAL — rendered as code text,
never linkified, never with the backticks silently dropped as if it were markup.

A dangling reference [[entities/x/ghost-page]] must still render as readable text, with no builder
anchor markup or stray backticks leaking into the visible page.
