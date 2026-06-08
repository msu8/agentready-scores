# AgentReady Central Repo Demo — Design Spec

**Date:** 2026-06-04  
**Status:** Approved  

---

## Overview

Build a working demo that runs the agentready assessment tool concurrently across a subset of `konflux-ci` org repos, stores results in a personal central GitHub repo, and ingests them into a local DevLake instance using the centralized submissions model from PR #95.

The system has three components:
1. **Local DevLake instance** — built natively with PR #94 + #95 from `CryptoRodeo/devlake`
2. **Central repo** (`Dannyb48/agentready-scores`) — stores assessment JSONs in `submissions/org/repo/` structure
3. **Runner** — Python script (local demo) + GitHub Actions workflows (production)

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│         Dannyb48/agentready-scores (GitHub)         │
│                                                     │
│  submissions/                                       │
│    konflux-ci/                                      │
│      devlake/                                       │
│        assessment-latest.json  ← symlink            │
│        assessment-20260604-120000.json              │
│      pipeline-service/                              │
│        assessment-latest.json  ← symlink            │
│        assessment-20260604-120000.json              │
│      ...                                            │
│                                                     │
│  runner/                                            │
│    assess.py          ← local + CI runner           │
│    repos.yaml         ← curated list for demo       │
│    requirements.txt                                 │
│    failed-repos.txt   ← generated on failures       │
│                                                     │
│  .github/workflows/                                 │
│    assess-manual.yml     ← workflow_dispatch        │
│    assess-scheduled.yml  ← cron (production)        │
└─────────────────────────────────────────────────────┘
          │ plugin reads via GitHub API
          ▼
┌─────────────────────────────────────────────────────┐
│         Local DevLake instance (x86_64)             │
│                                                     │
│  Plugin: agentready (PR #94 + #95)                  │
│  Scope config:                                      │
│    mode: submissions                                │
│    submissionsRepo: Dannyb48/agentready-scores      │
│    submissionsPath: submissions/konflux-ci          │
│    submissionsBranch: main                          │
│                                                     │
│  Grafana: fleet-overview, findings-analysis,        │
│           repo-detail (project-filtered via #94)    │
└─────────────────────────────────────────────────────┘
```

**Data flow:**
1. Runner discovers repos (from `repos.yaml` locally, GitHub API in CI)
2. Runner clones each repo and runs the agentready container concurrently via podman
3. Assessment JSONs + `assessment-latest.json` symlinks are committed to the central repo
4. DevLake agentready plugin reads from the central repo via GitHub API — no need for repos to store their own assessment files
5. Grafana dashboards show fleet-level and per-repo scores, filterable by DevLake project

---

## Part 1: Local DevLake Setup

### Prerequisites
- Go 1.21+ installed (`sudo dnf install golang`)
- podman + podman-compose installed
- `ghcr.io` login for agentready container image

### Steps

**1. Copy agentready dashboards into Grafana provisioning dir:**
```bash
cd /home/dbaez/Projects/devlake_tools/devlake
cp backend/plugins/agentready/grafana/*.json grafana/dashboards/
```

**2. Start infrastructure containers only:**
```bash
podman compose -f docker-compose-dev.yml up -d mysql grafana config-ui
```

**3. Pull PR #94 + #95 and build backend natively:**
```bash
git remote add cryptorodeo https://github.com/CryptoRodeo/devlake.git
git fetch cryptorodeo feat/agentready-submissions-onboarding
git checkout -b feat/agentready-submissions-onboarding cryptorodeo/feat/agentready-submissions-onboarding

cd backend
make go-dep
make build
make run   # foreground — streams logs
```

**Services after setup:**

| Service | How it runs | URL |
|---|---|---|
| MySQL | podman container | localhost:3306 |
| Grafana | podman container | localhost:3002 |
| config-ui | podman container | localhost:4000 |
| DevLake backend | native Go binary | localhost:8080 |

### DevLake Plugin Configuration

In the config-ui at `http://localhost:4000`:
1. Add a GitHub connection with a PAT (`repo` + `read:org` scopes)
2. Create a project (e.g. `konflux-ci`)
3. Add `Dannyb48/agentready-scores` as a scope
4. Configure the agentready scope config:
   - **Mode:** Submissions
   - **Submissions repo:** `Dannyb48/agentready-scores`
   - **Submissions branch:** `main`
   - **Submissions path:** `submissions/konflux-ci`
5. Trigger the agentready pipeline

---

## Part 2: Central Repo Structure

Repo: `Dannyb48/agentready-scores` (public GitHub repo)

```
submissions/
  konflux-ci/
    {repo-name}/
      assessment-latest.json          ← git symlink → assessment-YYYYMMDD-HHMMSS.json
      assessment-YYYYMMDD-HHMMSS.json ← actual assessment data
runner/
  assess.py
  repos.yaml
  requirements.txt
  failed-repos.txt                    ← generated, gitignored between runs
.github/
  workflows/
    assess-manual.yml
    assess-scheduled.yml
README.md
```

The `assessment-latest.json` symlink is how the DevLake plugin resolves the latest assessment. The GitHub Contents API auto-follows symlinks when the plugin fetches `assessment-latest.json`, returning the content of the pointed-to timestamped file.

Old timestamped files accumulate over runs, providing history.

---

## Part 3: Runner (`runner/assess.py`)

### Modes

| Flag | Behavior |
|---|---|
| `--mode demo` | Reads `runner/repos.yaml` — curated subset of repos |
| `--mode prod` | Discovers all public repos in `konflux-ci` org via GitHub API (paginated) |
| `--from-file PATH` | Assess only repos listed in a file (e.g. `failed-repos.txt` re-runs) |

### Other flags

| Flag | Default | Description |
|---|---|---|
| `--workers N` | 5 | Max concurrent assessments |
| `--retries N` | 1 | Retry attempts per failed repo |
| `--output-dir` | `submissions/` | Path to submissions dir in central repo |

### Per-repo worker flow

For each repo, a worker:
1. Clones `https://github.com/konflux-ci/{repo}` to a temp directory
2. Runs the agentready container:
   ```bash
   podman run --rm \
     --user $(id -u):$(id -g) --userns=keep-id \
     -e GIT_CONFIG_COUNT=1 \
     -e GIT_CONFIG_KEY_0=safe.directory \
     -e GIT_CONFIG_VALUE_0=/repo \
     -v {temp_clone}:/repo:ro,z \
     -v {temp_output}:/reports:z \
     ghcr.io/ambient-code/agentready:latest \
     assess /repo --output-dir /reports
   ```
3. Copies `assessment-YYYYMMDD-HHMMSS.json` from temp output into `submissions/konflux-ci/{repo}/`
4. Creates/updates the `assessment-latest.json` symlink
5. Cleans up temp clone and output

### Batch flow

```
1. Discover repos (repos.yaml or GitHub API)
2. Run batch with ThreadPoolExecutor (N workers)
3. Retry failed repos up to --retries times
4. git add . && git commit -m "chore: assess konflux-ci repos YYYY-MM-DD"
5. git push
6. Write still-failed repos to runner/failed-repos.txt
7. Print summary: X succeeded, Y failed
```

A single commit per run keeps history clean.

### `repos.yaml` format

```yaml
org: konflux-ci
repos:
  - devlake
  - pipeline-service
  - build-service
  - integration-service
  - release-service
```

### `requirements.txt`

```
requests>=2.31.0
PyYAML>=6.0
```

---

## Part 4: GitHub Actions Workflows

### Secrets required

| Secret | Purpose |
|---|---|
| `GHCR_TOKEN` | Pull `ghcr.io/ambient-code/agentready:latest` (private image) |
| `GH_TOKEN` | Discover all `konflux-ci` repos via GitHub API (prod mode) |
| `GITHUB_TOKEN` | Built-in — commit + push results to central repo |

### `assess-manual.yml` (demo)

Triggered via `workflow_dispatch`. Uses `repos.yaml`.

```yaml
on:
  workflow_dispatch:
    inputs:
      workers:
        description: 'Max concurrent workers'
        default: '5'
      retries:
        description: 'Retry attempts for failures'
        default: '1'
      from_file:
        description: 'Path to failed-repos.txt (optional re-run)'
        required: false
```

### `assess-scheduled.yml` (production)

Runs weekly on cron, also manually triggerable.

```yaml
on:
  schedule:
    - cron: '0 6 * * 1'  # Every Monday 6am UTC
  workflow_dispatch: {}
```

### Shared steps (both workflows)

1. Checkout central repo
2. Login to `ghcr.io` using `GHCR_TOKEN`
3. Install Python deps from `runner/requirements.txt`
4. Run `assess.py` with appropriate flags
5. Commit and push via `GITHUB_TOKEN`

---

## Out of Scope

- Assessing private `konflux-ci` repos (public repos only for this POC)
- Pruning old timestamped assessment files (accumulate as history)
- Automated DevLake pipeline triggering after runner completes

---

## Open Questions

- None — all design decisions resolved during brainstorming
