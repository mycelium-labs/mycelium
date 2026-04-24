# `incidents/public/`

Raw data ingested from public sources. Do not edit these files.
Tagging and taxonomy mapping happen in `../tagged/`.

## Layout

```
public/
├── github-issues/      # scraped by scripts/scrape_github_issues.py
│   ├── manifest.json   # last-scrape date + count per repo
│   └── {repo}/
│       └── YYYY-MM-DD.jsonl
├── aiid/               # AI Incident Database bulk dump (later)
├── aiaaic/             # AIAAIC repository dump (later)
├── benchmarks/         # failed trajectories from SWE-bench, tau-bench, etc (later)
└── press/              # curated press-reported incidents (later)
```

## Rules

- Raw data only. No transformation at ingest time.
- Each ingest run is dated. Never overwrite older snapshots.
- Commit raw data to the repo — it IS the research corpus.
- All sources must be free and permissively-licensed for internal research
  use. Cite + link every incident when it graduates to `tagged/`.
