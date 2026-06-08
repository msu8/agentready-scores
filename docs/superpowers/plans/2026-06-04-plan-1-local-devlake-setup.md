# Local DevLake Setup (PR #94 + #95) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Get a local DevLake instance running with the agentready submissions/central-repo model from PR #95, backed by native Go build + podman infrastructure containers.

**Architecture:** Infrastructure (MySQL, Grafana, config-ui) runs in podman containers. The DevLake Go backend is built and run natively on x86_64 to avoid the ARM64-only Dockerfile. PR #95 branch from CryptoRodeo's fork is checked out locally and compiled into the native binary.

**Tech Stack:** Go 1.21+, podman, podman-compose, make, MySQL 8, Grafana

---

## Prerequisites Check

Before starting, verify these are installed:

```bash
go version          # must be 1.21+
podman --version    # must be 5.x
podman-compose --version
```

If Go is missing: `sudo dnf install golang`

---

### Task 1: Copy agentready dashboards into Grafana provisioning

**Files:**
- Copy: `backend/plugins/agentready/grafana/*.json` → `grafana/dashboards/`

- [ ] **Step 1: Copy the three agentready dashboard JSONs**

```bash
cd /home/dbaez/Projects/devlake_tools/devlake
cp backend/plugins/agentready/grafana/fleet-overview.json grafana/dashboards/
cp backend/plugins/agentready/grafana/findings-analysis.json grafana/dashboards/
cp backend/plugins/agentready/grafana/repo-detail.json grafana/dashboards/
```

- [ ] **Step 2: Verify they're in place**

```bash
ls grafana/dashboards/ | grep -i agent
```

Expected output:
```
findings-analysis.json
fleet-overview.json
repo-detail.json
```

- [ ] **Step 3: Commit**

```bash
git add grafana/dashboards/
git commit -m "chore: add agentready dashboards to grafana provisioning"
```

---

### Task 2: Start infrastructure containers

**Services started:** MySQL, Grafana, config-ui (NOT the devlake backend — that runs natively)

- [ ] **Step 1: Start only the infrastructure services**

```bash
cd /home/dbaez/Projects/devlake_tools/devlake
podman compose -f docker-compose-dev.yml up -d mysql grafana config-ui
```

- [ ] **Step 2: Verify all three containers are running**

```bash
podman ps --format "table {{.Names}}\t{{.Status}}"
```

Expected output (all three showing `Up`):
```
NAMES                STATUS
devlake_mysql_1      Up X seconds
devlake_grafana_1    Up X seconds
devlake_config-ui_1  Up X seconds
```

- [ ] **Step 3: Verify MySQL is accepting connections**

```bash
podman exec devlake_mysql_1 mysql -umerico -pmerico -e "SELECT 1;" lake
```

Expected: `1` with no errors.

---

### Task 3: Fetch and checkout PR #94 + #95 branch

PR #95 (`feat/agentready-submissions-onboarding`) already includes PR #94's Grafana changes (they were merged into the branch). Only one checkout needed.

- [ ] **Step 1: Add CryptoRodeo's fork as a remote**

Run this in your terminal (requires network access outside the sandbox):

```bash
cd /home/dbaez/Projects/devlake_tools/devlake
git remote add cryptorodeo https://github.com/CryptoRodeo/devlake.git
```

- [ ] **Step 2: Fetch the PR branch**

```bash
git fetch cryptorodeo feat/agentready-submissions-onboarding
```

- [ ] **Step 3: Check out the branch**

```bash
git checkout -b feat/agentready-submissions-onboarding \
  cryptorodeo/feat/agentready-submissions-onboarding
```

- [ ] **Step 4: Verify the new files are present**

```bash
ls backend/plugins/agentready/tasks/submissions_collector.go
ls backend/plugins/agentready/models/migrationscripts/add_submissions_config.go
```

Both should exist with no "no such file" error.

---

### Task 4: Build the DevLake backend natively

- [ ] **Step 1: Install Go dependencies**

```bash
cd /home/dbaez/Projects/devlake_tools/devlake/backend
make go-dep
```

Expected: downloads modules, ends without error. Takes 1-3 minutes.

- [ ] **Step 2: Build all plugins and the server**

```bash
make build
```

Expected: compiles plugins into `bin/plugins/`, builds `bin/lake` server binary. Takes 3-8 minutes. Should end with no errors.

- [ ] **Step 3: Verify the binary exists**

```bash
ls -lh bin/lake
```

Expected: file exists, size > 10MB.

- [ ] **Step 4: Start the server**

```bash
make run
```

The server starts in the foreground. Watch for a log line like:
```
DevLake Server started at :8080
```

Keep this terminal open. Open a new terminal for subsequent steps.

---

### Task 5: Verify DevLake is up and agentready plugin is loaded

- [ ] **Step 1: Check the plugins API**

```bash
curl -s http://localhost:4000/api/plugins | python3 -m json.tool | grep -i agentready
```

Expected output includes:
```json
"plugin": "agentready"
```

- [ ] **Step 2: Check agentready tables exist in MySQL**

```bash
podman exec devlake_mysql_1 mysql -umerico -pmerico lake \
  -e "SHOW TABLES LIKE '_tool_agentready%';"
```

Expected tables:
```
_tool_agentready_assessments
_tool_agentready_findings
_tool_agentready_metrics
_tool_agentready_scope_configs
```

- [ ] **Step 3: Check submissions config columns exist (PR #95 migration)**

```bash
podman exec devlake_mysql_1 mysql -umerico -pmerico lake \
  -e "DESCRIBE _tool_agentready_scope_configs;"
```

Expected to include columns: `submissions_repo`, `submissions_path`, `submissions_branch`, `submissions_connection_id`

---

### Task 6: Configure GitHub connection and project in DevLake

- [ ] **Step 1: Open config-ui**

Navigate to `http://localhost:4000` in your browser.

- [ ] **Step 2: Add a GitHub connection**

Go to **Connections → GitHub → Add Connection**. Configure:
- **Name:** `GitHub`
- **Token:** A GitHub PAT with `repo` and `read:org` scopes
- **Endpoint:** `https://api.github.com`

Test the connection — it should show green.

- [ ] **Step 3: Create a project**

Go to **Projects → Create Project**. Name it `konflux-ci`.

- [ ] **Step 4: Add the agentready-scores repo as a scope**

In the project, add `Dannyb48/agentready-scores` as a GitHub scope.

- [ ] **Step 5: Configure the agentready scope config**

In the project's agentready settings, configure:
- **Mode:** Submissions (central repo)
- **Submissions Repo:** `Dannyb48/agentready-scores`
- **Submissions Branch:** `main`
- **Submissions Path:** `submissions/konflux-ci`
- **Submissions Connection:** select the GitHub connection created above

---

### Task 7: Verify Grafana dashboards appear

- [ ] **Step 1: Open Grafana**

Navigate to `http://localhost:3002` (or `http://localhost:4000/grafana/`).

- [ ] **Step 2: Check for agentready dashboards**

Go to **Dashboards → Browse**. Look for:
- `AgentReady Fleet Overview`
- `AgentReady Findings Analysis`
- `AgentReady Repository Detail`

All three should be visible (empty until a pipeline run ingests data).

---

**Plan 1 complete.** Once the runner (Plan 2) has populated `agentready-scores` with assessment JSONs and you trigger a DevLake pipeline run, the dashboards will show data.
