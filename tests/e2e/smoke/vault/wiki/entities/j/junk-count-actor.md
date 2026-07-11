---
type: actor
id: junk-count-actor
title: Junk Count Actor
recent_reports:
- sources/2026/07/some-report-path
sources:
- Smoke test fixture
---

# Junk Count Actor

LEGACY BAD DATA fixture: `recent_reports` holds a LIST of paths where a count belongs (the shape
the write path now rejects, but pre-gate pages like this exist). A numeric-sorted box must rank
this row BELOW every real count — never at the top (the Most-active live incident).
