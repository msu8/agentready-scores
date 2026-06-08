# GitHub Actions Workflows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Write two GitHub Actions workflows in the `agentready-scores` repo — one for manual demo runs (`workflow_dispatch`) and one for weekly automated production runs (`schedule`) — both using `assess.py` from Plan 2.

**Architecture:** Both workflows share the same steps: checkout → ghcr.io login → Python setup → run assess.py → push is handled by assess.py itself. The manual workflow uses `--mode demo` with configurable inputs; the scheduled workflow uses `--mode prod` to discover all public konflux-ci repos.

**Tech Stack:** GitHub Actions, Python 3.11, podman (pre-installed on `ubuntu-latest` runners via `runs-on: ubuntu-latest`)

All files in: `/home/dbaez/Projects/devlake_tools/agentready-scores/.github/workflows/`

---

### Task 1: Write `assess-manual.yml` (demo / workflow_dispatch)

**Files:**
- Write: `.github/workflows/assess-manual.yml`

- [ ] **Step 1: Write the manual workflow**

```yaml
# .github/workflows/assess-manual.yml
name: Assess repos (manual)

on:
  workflow_dispatch:
    inputs:
      workers:
        description: 'Max concurrent workers'
        default: '5'
        required: false
      retries:
        description: 'Retry attempts per failed repo'
        default: '1'
        required: false
      from_file:
        description: 'Path to failed-repos.txt to re-run (leave blank for demo mode)'
        required: false

jobs:
  assess:
    runs-on: ubuntu-latest
    permissions:
      contents: write  # needed to push commits back to the repo

    steps:
      - name: Checkout central repo
        uses: actions/checkout@v4
        with:
          token: ${{ secrets.GITHUB_TOKEN }}

      - name: Login to ghcr.io
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GHCR_TOKEN }}

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'
          cache-dependency-path: runner/requirements.txt

      - name: Install dependencies
        run: pip install -r runner/requirements.txt

      - name: Configure git for pushing
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"

      - name: Run assessments
        env:
          GH_TOKEN: ${{ secrets.GH_TOKEN }}
        run: |
          if [ -n "${{ inputs.from_file }}" ]; then
            python3 runner/assess.py \
              --from-file "${{ inputs.from_file }}" \
              --workers "${{ inputs.workers }}" \
              --retries "${{ inputs.retries }}"
          else
            python3 runner/assess.py \
              --mode demo \
              --workers "${{ inputs.workers }}" \
              --retries "${{ inputs.retries }}"
          fi

      - name: Upload failed repos artifact (if any)
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: failed-repos
          path: runner/failed-repos.txt
          if-no-files-found: ignore
```

- [ ] **Step 2: Verify it is valid YAML**

```bash
python3 -c "
import yaml
with open('.github/workflows/assess-manual.yml') as f:
    data = yaml.safe_load(f)
print('Valid YAML:', list(data.keys()))
"
```

Expected: `Valid YAML: ['name', 'on', 'jobs']`

---

### Task 2: Write `assess-scheduled.yml` (production / cron)

**Files:**
- Write: `.github/workflows/assess-scheduled.yml`

- [ ] **Step 1: Write the scheduled workflow**

```yaml
# .github/workflows/assess-scheduled.yml
name: Assess repos (scheduled)

on:
  schedule:
    - cron: '0 6 * * 1'  # Every Monday at 06:00 UTC
  workflow_dispatch: {}   # Also manually triggerable

jobs:
  assess:
    runs-on: ubuntu-latest
    permissions:
      contents: write

    steps:
      - name: Checkout central repo
        uses: actions/checkout@v4
        with:
          token: ${{ secrets.GITHUB_TOKEN }}

      - name: Login to ghcr.io
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GHCR_TOKEN }}

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'
          cache-dependency-path: runner/requirements.txt

      - name: Install dependencies
        run: pip install -r runner/requirements.txt

      - name: Configure git for pushing
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"

      - name: Run assessments (all konflux-ci public repos)
        env:
          GH_TOKEN: ${{ secrets.GH_TOKEN }}
        run: |
          python3 runner/assess.py \
            --mode prod \
            --workers 5 \
            --retries 1

      - name: Upload failed repos artifact (if any)
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: failed-repos
          path: runner/failed-repos.txt
          if-no-files-found: ignore

      - name: Fail job if failures remain
        if: always()
        run: |
          if [ -f runner/failed-repos.txt ]; then
            echo "Some repos failed assessment:"
            cat runner/failed-repos.txt
            exit 1
          fi
```

- [ ] **Step 2: Verify it is valid YAML**

```bash
python3 -c "
import yaml
with open('.github/workflows/assess-scheduled.yml') as f:
    data = yaml.safe_load(f)
print('Valid YAML:', list(data.keys()))
"
```

Expected: `Valid YAML: ['name', 'on', 'jobs']`

---

### Task 3: Add required secrets to the GitHub repo

These must be set in `Dannyb48/agentready-scores` → Settings → Secrets and variables → Actions.

- [ ] **Step 1: Add `GHCR_TOKEN`**

Go to: `https://github.com/Dannyb48/agentready-scores/settings/secrets/actions`

Add secret:
- **Name:** `GHCR_TOKEN`
- **Value:** A GitHub PAT with `read:packages` scope (to pull `ghcr.io/ambient-code/agentready:latest`)

- [ ] **Step 2: Add `GH_TOKEN`**

Add secret:
- **Name:** `GH_TOKEN`
- **Value:** A GitHub PAT with `public_repo` and `read:org` scopes (to clone repos and call the GitHub API for org repo discovery in prod mode)

Note: `GITHUB_TOKEN` is built-in and does not need to be added manually. It is used for committing back to the central repo.

---

### Task 4: Commit workflows and trigger a test run

- [ ] **Step 1: Commit both workflows**

```bash
cd /home/dbaez/Projects/devlake_tools/agentready-scores
git add .github/workflows/assess-manual.yml .github/workflows/assess-scheduled.yml
git commit -m "feat: add manual and scheduled assessment workflows"
git push
```

- [ ] **Step 2: Trigger the manual workflow**

Go to: `https://github.com/Dannyb48/agentready-scores/actions/workflows/assess-manual.yml`

Click **Run workflow** → leave defaults → click **Run workflow**.

- [ ] **Step 3: Monitor the run**

Watch the workflow run. Each repo should show `✓` or `✗` in the logs. The run completes when all repos are assessed and results are committed.

- [ ] **Step 4: Verify commits appear in the central repo**

Go to: `https://github.com/Dannyb48/agentready-scores/commits/main`

Expected: a commit like `chore: assess konflux-ci repos 2026-06-04 — devlake, pipeline-service, build-service (+5 more)`

- [ ] **Step 5: Verify submissions structure**

Go to: `https://github.com/Dannyb48/agentready-scores/tree/main/submissions/konflux-ci`

Expected: one directory per assessed repo, each containing `assessment-latest.json` (symlink) and `assessment-YYYYMMDD-HHMMSS.json`.

---

**Plan 3 complete.** The full pipeline is now operational:

1. Runner assesses repos and populates the central repo
2. DevLake pulls assessments from the central repo via the submissions plugin
3. Grafana dashboards show fleet-level AgentReady scores for konflux-ci
