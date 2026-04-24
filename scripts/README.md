# `scripts/`

Dev and data-ingestion tooling for the Mycelium repo.

---

## `scrape_github_issues.py`

Pulls issues (open + closed) from a target GitHub repo, buffers them locally
as JSONL, and pushes them to a **private Hugging Face dataset repo**.

The Hugging Face repo is the source of truth. The local directory
`incidents/public/github-issues/` is just a short-lived upload buffer (and is
gitignored).

This is v0 of the Mycelium threat-research pipeline — manual for now, will be
driven by a Claude skill later.

### One-time setup

#### 1. GitHub token

Create a personal access token at **https://github.com/settings/tokens**:

- *Generate new token (classic)*
- Name: `mycelium-scraper`
- Expiration: 90 days (or longer)
- Scope: `public_repo`
- Copy the token (starts with `ghp_...`).

```bash
export GITHUB_TOKEN=ghp_your_token_here
```

#### 2. Hugging Face token + dataset repo

1. Create a Hugging Face account: https://huggingface.co/join
2. Create a **write** token: https://huggingface.co/settings/tokens
   (New token → name `mycelium-scraper` → Type: *Write*)
3. Pick a repo slug. Two options:
   - Under your user: e.g. `your-hf-username/mycelium-agent-failures`
   - Under an org (recommended long-term): create an org
     `mycelium-labs`, then `mycelium-labs/agent-failures`

   You do **not** need to create the dataset in the HF UI — the scraper will
   create it (private) on first run if it doesn't exist.

```bash
export HF_TOKEN=hf_your_token_here
export MYCELIUM_HF_REPO=your-hf-username/mycelium-agent-failures
```

#### 3. Install the upload client

```bash
pip install huggingface_hub
# or: uv pip install huggingface_hub
```

#### 4. Persist across terminal sessions

On macOS zsh (default), append the three exports to `~/.zshrc`:

```bash
cat >> ~/.zshrc <<'EOF'

# Mycelium scraper
export GITHUB_TOKEN=ghp_your_token_here
export HF_TOKEN=hf_your_token_here
export MYCELIUM_HF_REPO=your-hf-username/mycelium-agent-failures
EOF

source ~/.zshrc
```

### Run

```bash
# Single repo — start here.
python scripts/scrape_github_issues.py langchain-ai/langchain

# Every repo listed (uncommented) in scripts/repos.txt
python scripts/scrape_github_issues.py --all

# Incremental: only issues updated since a given date
python scripts/scrape_github_issues.py langchain-ai/langchain --since 2026-01-01

# Skip the HF upload (local buffer only)
python scripts/scrape_github_issues.py langchain-ai/langchain --no-upload
```

First run of `langchain-ai/langchain` fetches ~several thousand issues and
takes a few minutes. The scraper:

1. Scrapes to `incidents/public/github-issues/{repo-name}/YYYY-MM-DD.jsonl`.
2. Updates `incidents/public/github-issues/manifest.json`.
3. If `HF_TOKEN` + `MYCELIUM_HF_REPO` are set, creates the dataset repo
   (private, idempotent), then pushes the JSONL + manifest in one commit.

### Layout on Hugging Face

```
{your-hf-repo}/
├── README.md                                 # dataset card (auto-seeded)
├── manifest.json                             # last-scrape date + count per repo
└── github-issues/
    └── {repo-name}/
        └── YYYY-MM-DD.jsonl                  # one GitHub issue per line, raw payload
```

The dataset is **private by default**. You can flip it public later from the
HF settings UI once the corpus matures.

### Notes

- Pull requests are automatically dropped (GitHub returns PRs under `/issues`).
- With a PAT you have 5000 req/hr; a full scrape of langchain is ~80 requests.
- Re-running on the same day overwrites that day's file (local + HF);
  previous days persist.
- `--since` uses the `updated_at` field, so re-running picks up edited issues
  as well as new ones.
- If the HF upload fails, the local buffer file still exists — re-run the same
  command (with `--since` if you want to skip re-scraping) to retry.

### What to do after a successful scrape

1. Spot-check on HF: open `https://huggingface.co/datasets/$MYCELIUM_HF_REPO`
   and confirm the file appeared.
2. Download a sample: `hf download "$MYCELIUM_HF_REPO" --repo-type dataset --include 'github-issues/langchain/*.jsonl' --local-dir /tmp/mycelium-sample`
3. Pick 50 random issues, read them, tag each against `incidents/tagged/AF-*.md`
   or mark "not a failure mode."
4. Log the session in `LOG.md`.
