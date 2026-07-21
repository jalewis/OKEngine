# Reference structured-source connectors

These manifests are zero-seed examples: installing OKEngine does not activate
them, create schedules, or make upstream requests. Copy only the selected YAML
files into a pack's `connectors/` directory, add an explicit no-agent cron, and
run `framework validate` before deployment.

The examples cover three primary-source shapes:

- `github-status-incidents.yaml` — a vendor-operated status/advisory poll;
- `federal-register-documents.yaml` — an official regulatory-publication poll;
- `sec-company-submissions.yaml` — keyed company/filing-history enrichment.

For SEC, pass a ten-digit zero-padded CIK and an operator-controlled User-Agent
that complies with SEC automated-access guidance, for example:

```text
python /opt/data/scripts/source_connector.py \
  --manifest /opt/data/config/connectors/sec-company-submissions.yaml \
  --param cik=0000320193 \
  --param 'user_agent=Example Research ops@example.org' \
  --state-root /opt/data/state/connectors \
  --archive-root /opt/vault/raw/connectors \
  --health-root /opt/data/connectors/health \
  --summary-only
```

FederalRegister.gov is an official OFR/GPO service but its XML presentation is
not the legal edition; consumers making legal decisions must follow each
record's official-PDF link to govinfo.gov. Licensing, redistribution, and
retention declarations are deliberately explicit in every manifest and should
be reviewed against the operator's intended use before activation.
