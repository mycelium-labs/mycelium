# Incidents

This folder contains real-world agent failure data, tagged and structured by failure mode.

## Structure

- `public/` - raw ingested incidents, unprocessed
- `tagged/` - incidents mapped to AF-001 through AF-009 taxonomy
- `pipeline/ingest.md` - notes on how data gets in

## How to Add an Entry

1. Drop the raw incident into `public/`
2. Tag it against the taxonomy in `incidents/tagged/`
3. Log it in `incidents/2026-04-log.md`
