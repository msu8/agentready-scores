# AgentReady Concurrent Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `runner/assess.py` — a Python script that runs the agentready container concurrently against konflux-ci repos and commits assessment JSONs + symlinks to the `submissions/konflux-ci/` directory of the central repo.

**Architecture:** `ThreadPoolExecutor` fans out across repos. Each worker clones the repo, runs the agentready podman container, extracts the output JSON, writes it and a symlink into `submissions/{org}/{repo}/`, then cleans up. After all workers finish, one git commit covers all changes. Failures are retried once, then written to `runner/failed-repos.txt`.

**Tech Stack:** Python 3.9+, `concurrent.futures.ThreadPoolExecutor`, `subprocess` (for podman + git), `requests` (GitHub API), `PyYAML`

All work is done in: `/home/dbaez/Projects/devlake_tools/agentready-scores/`

---

### Task 1: Write `runner/requirements.txt` and `runner/repos.yaml`

**Files:**
- Write: `runner/requirements.txt`
- Write: `runner/repos.yaml`

- [ ] **Step 1: Write requirements.txt**

```
# runner/requirements.txt
requests>=2.31.0
PyYAML>=6.0
```

- [ ] **Step 2: Write repos.yaml with a demo subset of konflux-ci repos**

```yaml
# runner/repos.yaml
org: konflux-ci
repos:
  - devlake
  - pipeline-service
  - build-service
  - integration-service
  - release-service
  - dora-metrics
  - mintmaker
  - build-definitions
```

- [ ] **Step 3: Install deps locally for development**

```bash
cd /home/dbaez/Projects/devlake_tools/agentready-scores
pip install -r runner/requirements.txt
```

- [ ] **Step 4: Commit**

```bash
git add runner/requirements.txt runner/repos.yaml
git commit -m "chore: add runner dependencies and demo repo list"
```

---

### Task 2: Write `runner/assess.py` — argument parsing and entrypoint

**Files:**
- Write: `runner/assess.py`

- [ ] **Step 1: Write the argument parser and main entrypoint**

```python
#!/usr/bin/env python3
"""
AgentReady concurrent assessment runner.

Modes:
  --mode demo   Read repos from runner/repos.yaml
  --mode prod   Discover all public repos in the org via GitHub API
  --from-file   Read repo names from a file (one per line, e.g. failed-repos.txt)
"""
import argparse
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent


def parse_args():
    parser = argparse.ArgumentParser(description="Run agentready assessments concurrently")
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--mode", choices=["demo", "prod"],
        help="demo: use repos.yaml; prod: discover all org repos via GitHub API"
    )
    mode_group.add_argument(
        "--from-file", metavar="PATH",
        help="Assess only repos listed in this file (one repo name per line)"
    )
    parser.add_argument(
        "--workers", type=int, default=5,
        help="Max concurrent assessments (default: 5)"
    )
    parser.add_argument(
        "--retries", type=int, default=1,
        help="Retry attempts per failed repo (default: 1)"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=REPO_ROOT / "submissions",
        help="Path to submissions directory (default: ../submissions)"
    )
    parser.add_argument(
        "--org", default=None,
        help="GitHub org to assess (overrides repos.yaml org in prod mode)"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    from runner_lib import (
        load_demo_repos,
        discover_prod_repos,
        load_repos_from_file,
        run_batch,
        commit_results,
        write_failed_repos,
    )

    # Discover repos
    if args.from_file:
        org, repos = load_repos_from_file(Path(args.from_file))
    elif args.mode == "demo":
        org, repos = load_demo_repos(SCRIPT_DIR / "repos.yaml")
    else:  # prod
        org = args.org or load_demo_repos(SCRIPT_DIR / "repos.yaml")[0]
        repos = discover_prod_repos(org)

    if not repos:
        print("No repos to assess. Exiting.")
        sys.exit(0)

    print(f"Assessing {len(repos)} repos in {org} with {args.workers} workers...")

    # Run batch with retries
    succeeded, failed = run_batch(
        org=org,
        repos=repos,
        output_dir=args.output_dir,
        workers=args.workers,
        retries=args.retries,
    )

    # Commit all results
    if succeeded:
        commit_results(REPO_ROOT, org, succeeded)

    # Write failures
    failed_file = SCRIPT_DIR / "failed-repos.txt"
    if failed:
        write_failed_repos(failed_file, org, failed)
        print(f"\n{len(failed)} repos failed. Written to {failed_file}")
    elif failed_file.exists():
        failed_file.unlink()  # Clean up stale failures file

    print(f"\nDone: {len(succeeded)} succeeded, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify the file is syntactically valid**

```bash
python3 -c "import ast; ast.parse(open('runner/assess.py').read()); print('OK')"
```

Expected: `OK`

---

### Task 3: Write `runner/runner_lib.py` — repo discovery functions

**Files:**
- Create: `runner/runner_lib.py`

- [ ] **Step 1: Write load_demo_repos and load_repos_from_file**

```python
# runner/runner_lib.py
import os
import yaml
import requests
from pathlib import Path
from typing import Tuple, List


def load_demo_repos(repos_yaml: Path) -> Tuple[str, List[str]]:
    """Load org and repo list from repos.yaml."""
    with open(repos_yaml) as f:
        data = yaml.safe_load(f)
    return data["org"], data["repos"]


def load_repos_from_file(path: Path) -> Tuple[str, List[str]]:
    """
    Load repos from a file. Format: one entry per line.
    Lines starting with '#' are comments.
    Lines can be 'org/repo' or just 'repo' (org read from first non-comment line if prefixed).
    Returns (org, [repo_names]).
    """
    lines = [l.strip() for l in path.read_text().splitlines()
             if l.strip() and not l.strip().startswith("#")]

    if not lines:
        return "", []

    # Detect if lines are 'org/repo' format
    if "/" in lines[0]:
        parts = lines[0].split("/", 1)
        org = parts[0]
        repos = [l.split("/", 1)[1] if "/" in l else l for l in lines]
    else:
        # Plain repo names — org must come from repos.yaml
        from pathlib import Path as P
        yaml_path = P(__file__).parent / "repos.yaml"
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        org = data["org"]
        repos = lines

    return org, repos
```

- [ ] **Step 2: Write discover_prod_repos**

```python
def discover_prod_repos(org: str) -> List[str]:
    """Discover all public repos in a GitHub org via the API."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    repos = []
    page = 1
    while True:
        resp = requests.get(
            f"https://api.github.com/orgs/{org}/repos",
            headers=headers,
            params={"type": "public", "per_page": 100, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        repos.extend(r["name"] for r in batch)
        page += 1

    print(f"Discovered {len(repos)} public repos in {org}")
    return repos
```

- [ ] **Step 3: Verify syntax**

```bash
python3 -c "import ast; ast.parse(open('runner/runner_lib.py').read()); print('OK')"
```

Expected: `OK`

---

### Task 4: Write `runner/runner_lib.py` — per-repo worker

- [ ] **Step 1: Add imports and worker function**

Add to `runner/runner_lib.py`:

```python
import subprocess
import tempfile
import shutil
import glob
from datetime import datetime
```

Then add:

```python
def assess_repo(org: str, repo: str, output_dir: Path) -> str:
    """
    Clone repo, run agentready container, extract JSON, write to submissions dir.
    Returns the path of the assessment JSON written, or raises on failure.
    """
    repo_submissions_dir = output_dir / org / repo
    repo_submissions_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"agentready-{repo}-") as tmp:
        clone_dir = Path(tmp) / "repo"
        output_tmp = Path(tmp) / "output"
        output_tmp.mkdir()

        # Clone the repo (shallow, no blobs we don't need)
        subprocess.run(
            ["git", "clone", "--depth=1",
             f"https://github.com/{org}/{repo}.git",
             str(clone_dir)],
            check=True, capture_output=True, timeout=120,
        )

        uid = subprocess.check_output(["id", "-u"]).decode().strip()
        gid = subprocess.check_output(["id", "-g"]).decode().strip()

        # Run agentready container
        subprocess.run(
            [
                "podman", "run", "--rm",
                "--user", f"{uid}:{gid}",
                "--userns=keep-id",
                "-e", "GIT_CONFIG_COUNT=1",
                "-e", "GIT_CONFIG_KEY_0=safe.directory",
                "-e", "GIT_CONFIG_VALUE_0=/repo",
                "-v", f"{clone_dir}:/repo:ro,z",
                "-v", f"{output_tmp}:/reports:z",
                "ghcr.io/ambient-code/agentready:latest",
                "assess", "/repo", "--output-dir", "/reports",
            ],
            check=True, capture_output=True, timeout=600,
        )

        # Find the assessment JSON (assessment-YYYYMMDD-HHMMSS.json)
        json_files = glob.glob(str(output_tmp / "assessment-*.json"))
        if not json_files:
            raise FileNotFoundError(f"No assessment JSON found in agentready output for {repo}")

        # Take the most recent if multiple
        json_files.sort()
        src_json = Path(json_files[-1])
        dest_json = repo_submissions_dir / src_json.name

        shutil.copy2(src_json, dest_json)

        # Create/update the assessment-latest.json symlink
        symlink = repo_submissions_dir / "assessment-latest.json"
        if symlink.exists() or symlink.is_symlink():
            symlink.unlink()
        symlink.symlink_to(src_json.name)

        return str(dest_json)
```

- [ ] **Step 2: Verify syntax**

```bash
python3 -c "import ast; ast.parse(open('runner/runner_lib.py').read()); print('OK')"
```

Expected: `OK`

---

### Task 5: Write `runner/runner_lib.py` — batch runner, commit, and write failures

- [ ] **Step 1: Add run_batch function**

Add to `runner/runner_lib.py`:

```python
from concurrent.futures import ThreadPoolExecutor, as_completed


def run_batch(
    org: str,
    repos: List[str],
    output_dir: Path,
    workers: int,
    retries: int,
) -> Tuple[List[str], List[str]]:
    """
    Run assessments concurrently. Returns (succeeded_repos, failed_repos).
    Retries failed repos up to `retries` times.
    """
    succeeded = []
    failed = list(repos)  # start with all as failed, move to succeeded on success

    for attempt in range(retries + 1):
        if not failed:
            break
        if attempt > 0:
            print(f"\nRetry attempt {attempt} for {len(failed)} repos...")

        to_try = list(failed)
        failed = []

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(assess_repo, org, repo, output_dir): repo
                for repo in to_try
            }
            for future in as_completed(futures):
                repo = futures[future]
                try:
                    result = future.result()
                    print(f"  ✓ {org}/{repo} → {result}")
                    succeeded.append(repo)
                except Exception as e:
                    print(f"  ✗ {org}/{repo}: {e}")
                    failed.append(repo)

    return succeeded, failed
```

- [ ] **Step 2: Add commit_results function**

```python
def commit_results(repo_root: Path, org: str, repos: List[str]) -> None:
    """Stage and commit all new assessment files in one commit."""
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    repo_list = ", ".join(repos[:5])
    if len(repos) > 5:
        repo_list += f" (+{len(repos) - 5} more)"

    subprocess.run(
        ["git", "add", "submissions/"],
        cwd=repo_root, check=True,
    )
    subprocess.run(
        ["git", "commit", "-m",
         f"chore: assess {org} repos {date_str} — {repo_list}"],
        cwd=repo_root, check=True,
    )
    subprocess.run(
        ["git", "push"],
        cwd=repo_root, check=True,
    )
    print(f"\nCommitted and pushed {len(repos)} assessment(s).")
```

- [ ] **Step 3: Add write_failed_repos function**

```python
def write_failed_repos(path: Path, org: str, repos: List[str]) -> None:
    """Write failed repo names to a file for re-running."""
    lines = [f"# Failed repos from {datetime.utcnow().isoformat()}"]
    lines += [f"{org}/{r}" for r in repos]
    path.write_text("\n".join(lines) + "\n")
```

- [ ] **Step 4: Verify the full file parses**

```bash
python3 -c "import ast; ast.parse(open('runner/runner_lib.py').read()); print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Commit runner files**

```bash
cd /home/dbaez/Projects/devlake_tools/agentready-scores
git add runner/assess.py runner/runner_lib.py
git commit -m "feat: add concurrent agentready assessment runner"
```

---

### Task 6: Test the runner in demo mode (dry run verification)

Before running with podman (requires ghcr.io login), verify the CLI and discovery logic work.

- [ ] **Step 1: Test argument parsing**

```bash
cd /home/dbaez/Projects/devlake_tools/agentready-scores
python3 runner/assess.py --help
```

Expected: shows usage with `--mode`, `--from-file`, `--workers`, `--retries` options.

- [ ] **Step 2: Test repo discovery (demo mode)**

```bash
python3 -c "
from pathlib import Path
import sys; sys.path.insert(0, 'runner')
from runner_lib import load_demo_repos
org, repos = load_demo_repos(Path('runner/repos.yaml'))
print(f'org={org}, repos={repos}')
"
```

Expected: prints `org=konflux-ci` and the list of repos from `repos.yaml`.

- [ ] **Step 3: Test from-file loading**

```bash
echo "konflux-ci/devlake" > /tmp/test-repos.txt
python3 -c "
from pathlib import Path
import sys; sys.path.insert(0, 'runner')
from runner_lib import load_repos_from_file
org, repos = load_repos_from_file(Path('/tmp/test-repos.txt'))
print(f'org={org}, repos={repos}')
"
```

Expected: `org=konflux-ci, repos=['devlake']`

---

### Task 7: Run assessment against dora-metrics (first real test)

This requires being logged into `ghcr.io`. Run in your terminal.

- [ ] **Step 1: Log in to ghcr.io**

```bash
podman login ghcr.io
# Username: your GitHub username
# Password: GitHub PAT with read:packages scope
```

- [ ] **Step 2: Test a single repo assessment directly**

```bash
cd /home/dbaez/Projects/devlake_tools/agentready-scores
python3 -c "
from pathlib import Path
import sys; sys.path.insert(0, 'runner')
from runner_lib import assess_repo
result = assess_repo('Dannyb48', 'dora-metrics', Path('submissions'))
print('Success:', result)
"
```

Expected: prints path to the created assessment JSON under `submissions/Dannyb48/dora-metrics/`.

- [ ] **Step 3: Verify the symlink was created**

```bash
ls -la submissions/Dannyb48/dora-metrics/
```

Expected:
```
assessment-YYYYMMDD-HHMMSS.json
assessment-latest.json -> assessment-YYYYMMDD-HHMMSS.json
```

- [ ] **Step 4: Commit this test assessment**

```bash
git add submissions/
git commit -m "chore: add test assessment for Dannyb48/dora-metrics"
git push
```

---

### Task 8: Run demo mode against konflux-ci repos

- [ ] **Step 1: Run the full demo**

```bash
cd /home/dbaez/Projects/devlake_tools/agentready-scores
python3 runner/assess.py --mode demo --workers 3
```

Watch for `✓` and `✗` lines per repo. This will take several minutes.

- [ ] **Step 2: Verify submissions directory was populated**

```bash
find submissions/konflux-ci -name "assessment-latest.json" | sort
```

Expected: one symlink per successfully assessed repo.

- [ ] **Step 3: If there are failures, re-run from failed-repos.txt**

```bash
python3 runner/assess.py --from-file runner/failed-repos.txt
```

---

**Plan 2 complete.** The central repo now has assessment data. Proceed to Plan 3 (GitHub Actions) and then trigger a DevLake pipeline run to see data in Grafana.
