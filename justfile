# agentready-scores task runner
# Run `just --list` to see available recipes

# Run unit tests
test *args='':
    uv run pytest tests/ -v {{args}}

# Run e2e tests (requires GH_TOKEN in .env and podman)
test-e2e *args='':
    uv run pytest tests/test_e2e.py --run-e2e -v {{args}}

# Run all tests (unit + e2e)
test-all *args='':
    uv run pytest tests/ --run-e2e -v {{args}}

# Assess repos from an org YAML file
assess +files:
    uv run runner/assess.py --from-file {{files}}

# Assess all orgs in runner/orgs/
assess-all:
    uv run runner/assess.py --from-file runner/orgs/*.yaml

# Discover and assess all public repos in an org
assess-org org:
    uv run runner/assess.py --org {{org}}

# Re-run failed repos for an org
assess-retry org:
    uv run runner/assess.py --from-file runner/status/failed-{{org}}.yaml

# Collect summary and emit GHA annotations
collect-summary:
    uv run runner/collect_summary.py

# Validate GH_TOKEN
validate-token:
    uv run runner/collect_summary.py --validate-token
