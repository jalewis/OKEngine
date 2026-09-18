# Estimative language

OKEngine represents three different analytical dimensions with different fields. They are not
interchangeable:

- `estimative_probability` says how likely the proposition itself is.
- evidence confidence says how strongly the available evidence supports a judgment. Packs may
  model this with their existing `confidence` or `confidence_band` contract.
- attribution status says what relationship is being asserted or assessed. It remains a domain
  vocabulary (for example, an attribution pack's `attribution_status`).

Setting one must never manufacture either of the others. In particular, high confidence does not
mean that an event is likely, and “likely” does not mean the analyst has high confidence.

## Universal probability vocabulary

The base schema adopts the seven probability bands in the US Intelligence Community's public
[ICD 203: Analytic Standards](https://www.dni.gov/files/documents/ICD/ICD-203.pdf):

| Canonical value | Probability band | Meaning-preserving alias |
| --- | ---: | --- |
| `almost-no-chance` | 01–05% | `remote`, `almost no chance` |
| `very-unlikely` | 05–20% | `highly-improbable`, `highly improbable`, `very unlikely` |
| `unlikely` | 20–45% | `improbable` |
| `roughly-even-chance` | 45–55% | `roughly-even-odds`, spaced forms |
| `likely` | 55–80% | `probable` |
| `very-likely` | 80–95% | `highly-probable`, spaced forms |
| `almost-certain` | 95–99% | `nearly-certain`, `almost-certainly`, spaced forms |

The field is optional and engine-owned. A pack adopts it by writing the field; it does not repeat
or replace the enum. The vocabulary is closed so composition cannot change the meaning of a value.
Existing `confidence` and `confidence_band` fields remain valid and are not migrated automatically.

## Normalization and migration

Aliases are deliberately narrow. Terms such as `possible`, `medium`, `maybe`, and bare percentages
are ambiguous without a governed interpretation and therefore have no alias. Normalization never
uses lexical similarity, numeric rounding, or a neighboring confidence scale to infer a value.

Audit a vault before writing:

```sh
python scripts/normalize_vocabulary.py --vault /path/to/vault --tree wiki \
  --field estimative_probability --schema-aliases
```

The command is dry-run-first. It previews exact alias rewrites and reports every undeclared value
as `UNPARSEABLE / AMBIGUOUS`, leaving those values untouched. After a human resolves that report,
rerun with `--apply`. An explicit reviewed `--map OLD=NEW` may supplement the declared aliases for
that one migration; it does not expand the schema contract.
