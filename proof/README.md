# proof/

AF-006 proof suite vendored as a **git submodule**: [agent-test-AF006](https://github.com/mycelium-labs/agent-test-AF006).

CI checks out this repo with `submodules: recursive` so the default `GITHUB_TOKEN` can access the private submodule in the same org (no cross-repo checkout).

## Bump the proof suite

```bash
cd proof/agent-test-AF006
git fetch origin
git checkout <new-sha>
cd ../..
git add proof/agent-test-AF006
# Also update AGENT_TEST_AF006_REF in .github/workflows/proof.yml
git commit -m "chore: bump agent-test-AF006 proof submodule"
```

## Local run

```bash
export MYCELIUM_SDK="$(pwd)/sdk"
cd proof/agent-test-AF006
pip install -e "$MYCELIUM_SDK" pytest pytest-asyncio hypothesis psutil
pytest tests/test_af006_*.py tests/real_issues/ -q
```

## If CI cannot clone the submodule

Org admin: **Settings → Actions → General →** allow workflows to access repositories in the organization.

Or add a PAT with `repo` scope as secret `MYCELIUM_CI_PAT` and set `token: ${{ secrets.MYCELIUM_CI_PAT }}` in `.github/workflows/proof.yml`.
