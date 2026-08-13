# agentready-scores

A repository for collecting [AgentReady](https://github.com/ambient-code/agentready) AI readiness scores across one or more GitHub organizations and ingesting them into [DevLake](https://github.com/konflux-ci/devlake) for visualization.

## Structure

```
submissions/
  {org}/
    {repo}/
      assessment-YYYYMMDD-HHMMSS.json  ← actual assessment data
      assessment-latest.json           ← symlink to latest assessment
runner/
  assess.py        ← concurrent assessment runner (local + CI)
  runner_lib.py    ← core library (discovery, assessment, git commit)
  collect_summary.py ← CI summary + GHA annotations
  requirements.txt
  orgs/
    example.yaml   ← copy and rename for each org you want to assess
    {org}.yaml     ← your org config (org name + optional exclude list)
  status/
    failed-{org}.yaml       ← repos that failed assessment
    inaccessible-{org}.yaml ← repos the token can't reach
tests/
  test_runner_lib.py
  test_collect_summary.py
  test_e2e.py      ← end-to-end tests (requires podman + GH_TOKEN)
  conftest.py
.github/workflows/
  assess-manual.yml    ← manual trigger with configurable inputs
  assess-scheduled.yml ← weekly cron for all orgs in runner/orgs/
```

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — Python package manager
- [just](https://just.systems/) — task runner (`brew install just`)
- [direnv](https://direnv.net/) — automatic env loading (`brew install direnv`)
- [podman](https://podman.io/) — container runtime (for assessments and e2e tests)

## Setup

1. Copy `runner/orgs/example.yaml` → `runner/orgs/{your-org}.yaml` and fill in your org name
2. Set up local secrets:
   ```bash
   cp env.example .env
   # Edit .env — fill in your GH_TOKEN
   direnv allow
   ```
3. Add repository secrets (for CI):
   - `GHCR_TOKEN` — token to pull the `ghcr.io/ambient-code/agentready` image
   - `GH_TOKEN` — GitHub token with `repo` read + `contents: write` access
4. *(Optional)* Enable Slack failure notifications:
   - Secret: `SLACK_WEBHOOK_URL` — your Slack incoming webhook URL
   - Variable: `SLACK_NOTIFICATIONS` = `true`
5. Run the **Assess repos (manual)** workflow to generate your first assessments
6. Point your DevLake AgentReady connection at this repo (`submissions/` path)

## Running Locally

Run `just --list` to see all available recipes.

```bash
# Assess repos listed in an org YAML file
just assess runner/orgs/{your-org}.yaml

# Assess multiple orgs at once
just assess runner/orgs/*.yaml

# Assess all configured orgs
just assess-all

# Discover and assess ALL public repos in an org (no YAML needed)
just assess-org your-org-name

# Re-run repos that failed a previous run
just assess-retry your-org-name

# Validate your GH_TOKEN
just validate-token
```

## Development

```bash
# Run unit tests
just test

# Run e2e tests (requires podman + GH_TOKEN in .env)
just test-e2e

# Run all tests (unit + e2e)
just test-all
```

## Org YAML format

```yaml
org: your-org-name

# repos:           # optional — if omitted, all public repos are discovered
#   - repo-a
#   - repo-b

# exclude:         # optional — always skip these repos
#   - .github
#   - .fullsend
#   - archived-repo

# default_config: configs/your-org-default-config.yaml  # optional — see below
```

## Fallback config for missing ADRs

A repo's `architecture_decisions` attribute scores 0 if it has no ADR docs of
its own. To avoid that across a whole org, point `default_config:` at a
shared config file (based on `runner/orgs/default-config.yaml.template`) that
sets an `adr_source: {repo, path}` — this is applied to any repo in that org
that doesn't have its own config (`.agentready/config/.agentready-config.yaml`
or `.agentready-config.yaml`).

1. Copy `runner/orgs/default-config.yaml.template` → e.g.
   `runner/configs/{your-org}-default-config.yaml` and fill in the repo/path
   that holds your shared ADRs.
2. In `runner/orgs/{your-org}.yaml`, add:
   ```yaml
   default_config: configs/{your-org}-default-config.yaml
   ```
3. Run the assessment as normal — repos with their own config are untouched;
   repos with none get the fallback `adr_source` injected automatically.

## Failures

The runner distinguishes two kinds of problems:

| File | Cause | Retried? | Fails workflow? |
|------|-------|----------|-----------------|
| `runner/status/failed-{org}.yaml` | Assessment error (timeout, container crash, missing output) | Yes (`--retries`) | Yes |
| `runner/status/inaccessible-{org}.yaml` | Token cannot reach the repo (private, 403/404) | No | No |

Both files use the same YAML format as org configs. On a fully clean run the corresponding file is removed.

**Re-run failures locally:**

```bash
just assess-retry {your-org}
```

**Re-run failures via GitHub Actions:**

1. Go to **Actions → Assess repos (manual) → Run workflow**
2. In the `from_file` field enter the path to the failure file, e.g.:
   ```
   runner/status/failed-konflux-ci.yaml
   ```
3. Click **Run workflow** — only the previously failed repos will be assessed

Inaccessible repos should not be re-run — they require a token with broader access or the repo to be made public.

## Editing an org YAML

Org config files live in `runner/orgs/{org}.yaml`.

```yaml
org: your-org-name

# repos:           # pin to a specific list — omit to discover all public repos
#   - repo-a
#   - repo-b

exclude:           # always skip these (hidden dirs, archived repos, etc.)
  - .github
  - .fullsend
```

**Adding repos:** either add them to the `repos` list (pinned mode) or remove them from `exclude` (discovery mode).

**Excluding repos:** add the repo name to `exclude`. This persists across every run including scheduled ones.

**Switching from pinned to discovery mode:** delete the `repos` key entirely — the runner will call the GitHub API to find all public repos in `org`, minus anything in `exclude`.

After editing, commit and push the YAML file. The next scheduled run or manual dispatch will pick up the changes automatically.

## DevLake Integration

Configure an **AgentReady connection** in DevLake pointing to this repo:

| Field | Value |
|-------|-------|
| Submissions Repo | `your-org/agentready-scores` |
| Submissions Path | `submissions` |
| Branch | `main` |

DevLake discovers all `{org}/{repo}` scopes from the submissions tree and ingests the latest assessment for each.

## Notifications

Slack failure notifications are sent when `SLACK_NOTIFICATIONS = 'true'` is set as a repository variable and `SLACK_WEBHOOK_URL` is configured as a secret. Notifications fire on both manual and scheduled workflow failures.

The scheduled workflow also sends a batched Slack message whenever any repository gets its first-ever successful score in that run, and emits a `::notice::` GitHub Actions annotation per newly-scored repo on both workflows. New-repo notifications share the same `SLACK_NOTIFICATIONS`/`SLACK_WEBHOOK_URL` configuration as failure notifications — there is no separate toggle, and no manual-workflow Slack message for new repos.
