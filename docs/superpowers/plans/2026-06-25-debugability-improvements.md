# Debugability Improvements — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add structured error capture, coarse failure categorization, and GHA annotations to the assessment pipeline so operators can diagnose failures from the CI run summary without re-running.

**Architecture:** `assess_repo()` catches exceptions and returns `AssessmentResult` dataclass objects instead of strings. `assess.py` writes transient `errors-<org>.json` files. `collect_summary.py` reads those files and emits `::error`/`::warning` GHA annotations.

**Tech Stack:** Python 3.11, pytest, PyYAML, requests, direnv (e2e secrets via `.envrc`), podman (e2e only)

## Global Constraints

- Python 3.11+ (workflow uses `setup-python` with `3.11`)
- Dependencies limited to `runner/requirements.txt`
- Tests run locally with `uv run pytest`
- `failed-*.yaml` and `inaccessible-*.yaml` stay lean (org + repo list only) — no error details in YAML
- Workflow YAML files (`.github/workflows/`) are NOT modified
- `errors-*.json` files are transient — never committed or uploaded
- All new files go under `runner/` or `tests/` — nothing in repo root except `env.example`
- `AssessmentResult.message` capped at 500 characters

---

### Task 1: Add `AssessmentResult` dataclass and update `check_repo_access()` to 3-way return

**Files:**
- Modify: `runner/runner_lib.py:1-12` (add `dataclasses` import), `runner/runner_lib.py:155-169` (rewrite `check_repo_access`)
- Modify: `tests/test_runner_lib.py` (update existing tests, add new ones)

**Interfaces:**
- Consumes: nothing from other tasks
- Produces:
  - `AssessmentResult` dataclass with fields: `repo: str`, `status: str`, `category: str`, `message: str`, `output_path: str`
  - `check_repo_access(org: str, repo: str) -> str` returning `"accessible"`, `"forbidden"`, or `"not_found"`

- [ ] **Step 1: Write failing tests for `AssessmentResult` and updated `check_repo_access`**

Add to `tests/test_runner_lib.py`, replacing the existing import line and `TestCheckRepoAccess` class:

```python
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from runner_lib import AssessmentResult, check_repo_access, run_batch


class TestAssessmentResult:
    def test_dataclass_fields(self):
        r = AssessmentResult(
            repo="my-repo",
            status="failed",
            category="clone_failure",
            message="fatal: not found",
            output_path="",
        )
        assert r.repo == "my-repo"
        assert r.status == "failed"
        assert r.category == "clone_failure"
        assert r.message == "fatal: not found"
        assert r.output_path == ""

    def test_succeeded_result(self):
        r = AssessmentResult(
            repo="good-repo",
            status="succeeded",
            category="",
            message="",
            output_path="/tmp/result.json",
        )
        assert r.status == "succeeded"
        assert r.output_path == "/tmp/result.json"


class TestCheckRepoAccess:
    def test_accessible_repo(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("runner_lib.requests.head", return_value=mock_resp) as mock_head:
            assert check_repo_access("myorg", "myrepo") == "accessible"
            mock_head.assert_called_once()
            url = mock_head.call_args[0][0]
            assert url == "https://api.github.com/repos/myorg/myrepo"

    def test_forbidden_repo(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        with patch("runner_lib.requests.head", return_value=mock_resp):
            assert check_repo_access("myorg", "forbidden-repo") == "forbidden"

    def test_not_found_repo(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch("runner_lib.requests.head", return_value=mock_resp):
            assert check_repo_access("myorg", "missing-repo") == "not_found"

    def test_network_error_returns_not_found(self):
        import requests as req
        with patch("runner_lib.requests.head", side_effect=req.ConnectionError):
            assert check_repo_access("myorg", "myrepo") == "not_found"

    def test_uses_gh_token(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("runner_lib.requests.head", return_value=mock_resp) as mock_head, \
             patch.dict("os.environ", {"GH_TOKEN": "tok123"}):
            check_repo_access("o", "r")
            headers = mock_head.call_args[1]["headers"]
            assert headers["Authorization"] == "Bearer tok123"

    def test_no_token_still_works(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("runner_lib.requests.head", return_value=mock_resp) as mock_head, \
             patch.dict("os.environ", {}, clear=True):
            check_repo_access("o", "r")
            headers = mock_head.call_args[1]["headers"]
            assert "Authorization" not in headers
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_runner_lib.py::TestAssessmentResult tests/test_runner_lib.py::TestCheckRepoAccess -v`
Expected: FAIL — `ImportError: cannot import name 'AssessmentResult'` and `check_repo_access` returns `bool` instead of `str`

- [ ] **Step 3: Add `AssessmentResult` dataclass to `runner_lib.py`**

Add `import dataclasses` to the imports at the top of `runner/runner_lib.py` (line 1, alongside `import glob`). Then add the dataclass after the `SchemaError` class (after line 19):

```python
@dataclasses.dataclass
class AssessmentResult:
    repo: str
    status: str
    category: str
    message: str
    output_path: str
```

- [ ] **Step 4: Rewrite `check_repo_access()` for 3-way return**

Replace the existing `check_repo_access` function (lines 155-169) with:

```python
def check_repo_access(org: str, repo: str) -> str:
    """Return 'accessible', 'forbidden', or 'not_found'."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.head(
            f"https://api.github.com/repos/{org}/{repo}",
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 200:
            return "accessible"
        if resp.status_code == 403:
            return "forbidden"
        return "not_found"
    except requests.RequestException:
        return "not_found"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_runner_lib.py::TestAssessmentResult tests/test_runner_lib.py::TestCheckRepoAccess -v`
Expected: 8 PASSED

- [ ] **Step 6: Run all tests to check for regressions**

Run: `uv run pytest tests/ -v`
Expected: `TestRunBatchInaccessible` will fail because `run_batch` calls `assess_repo` which calls `check_repo_access`, and the mock in `TestRunBatchInaccessible` returns `"skipped:inaccessible"` as before — those tests still pass because `run_batch` checks for that string return, not the access check directly. If any test fails due to the `check_repo_access` return type change, fix it now.

- [ ] **Step 7: Commit**

```bash
git add runner/runner_lib.py tests/test_runner_lib.py
git commit -m "feat(runner): add AssessmentResult dataclass, make check_repo_access 3-way"
```

---

### Task 2: Rewrite `assess_repo()` to return `AssessmentResult` and update `run_batch()`

**Files:**
- Modify: `runner/runner_lib.py:184-327` (`assess_repo` and `run_batch`)
- Modify: `tests/test_runner_lib.py` (add `TestAssessRepo`, update `TestRunBatchInaccessible`)

**Interfaces:**
- Consumes: `AssessmentResult` dataclass, `check_repo_access() -> str` (from Task 1)
- Produces:
  - `assess_repo(org: str, repo: str, output_dir: Path) -> AssessmentResult`
  - `run_batch(org, repos, output_dir, workers, retries) -> Tuple[List[str], List[str], List[str], List[AssessmentResult]]` — 4th element is all results

- [ ] **Step 1: Write failing tests for `assess_repo` returning `AssessmentResult`**

Add to `tests/test_runner_lib.py`:

```python
import subprocess

from runner_lib import assess_repo


class TestAssessRepo:
    def test_auth_forbidden(self, tmp_path):
        with patch("runner_lib.check_repo_access", return_value="forbidden"):
            result = assess_repo("myorg", "secret-repo", tmp_path)
        assert isinstance(result, AssessmentResult)
        assert result.status == "skipped"
        assert result.category == "auth_forbidden"
        assert result.repo == "secret-repo"

    def test_auth_not_found(self, tmp_path):
        with patch("runner_lib.check_repo_access", return_value="not_found"):
            result = assess_repo("myorg", "gone-repo", tmp_path)
        assert result.status == "skipped"
        assert result.category == "auth_not_found"

    def test_clone_failure(self, tmp_path):
        err = subprocess.CalledProcessError(128, "git", stderr=b"fatal: repo not found\n")
        with patch("runner_lib.check_repo_access", return_value="accessible"), \
             patch("runner_lib.subprocess.run", side_effect=err):
            result = assess_repo("myorg", "bad-repo", tmp_path)
        assert result.status == "failed"
        assert result.category == "clone_failure"
        assert "fatal: repo not found" in result.message

    def test_clone_timeout(self, tmp_path):
        err = subprocess.TimeoutExpired("git", 120)
        with patch("runner_lib.check_repo_access", return_value="accessible"), \
             patch("runner_lib.subprocess.run", side_effect=err):
            result = assess_repo("myorg", "slow-repo", tmp_path)
        assert result.status == "failed"
        assert result.category == "clone_failure"
        assert "timed out" in result.message.lower()

    def test_clone_failure_message_capped_at_500(self, tmp_path):
        long_stderr = b"x" * 1000
        err = subprocess.CalledProcessError(128, "git", stderr=long_stderr)
        with patch("runner_lib.check_repo_access", return_value="accessible"), \
             patch("runner_lib.subprocess.run", side_effect=err):
            result = assess_repo("myorg", "bad-repo", tmp_path)
        assert len(result.message) <= 500

    def test_container_failure(self, tmp_path):
        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()

        call_count = {"n": 0}

        def mock_run(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return MagicMock(returncode=0)  # git clone succeeds
            if call_count["n"] == 2:
                # git rev-parse HEAD succeeds
                m = MagicMock()
                m.returncode = 0
                m.stdout = b"abc123\n"
                return m
            if call_count["n"] <= 4:
                # id -u and id -g
                return MagicMock(stdout=b"1000\n", decode=lambda: "1000")
            # podman run fails
            raise subprocess.CalledProcessError(1, "podman", stderr=b"container crashed\n")

        with patch("runner_lib.check_repo_access", return_value="accessible"), \
             patch("runner_lib.subprocess.run", side_effect=mock_run), \
             patch("runner_lib.subprocess.check_output", return_value=b"1000\n"), \
             patch("runner_lib._prior_commit_hash", return_value=None), \
             patch("tempfile.TemporaryDirectory") as mock_tmp:
            mock_tmp.return_value.__enter__ = MagicMock(return_value=str(tmp_path))
            mock_tmp.return_value.__exit__ = MagicMock(return_value=False)
            result = assess_repo("myorg", "crash-repo", tmp_path)
        assert result.status == "failed"
        assert result.category == "container_failure"
        assert "container crashed" in result.message
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_runner_lib.py::TestAssessRepo -v`
Expected: FAIL — `assess_repo` still returns `str`

- [ ] **Step 3: Rewrite `assess_repo()` to return `AssessmentResult`**

Replace the `assess_repo` function body in `runner/runner_lib.py` (lines 184-276). The new implementation:

```python
def assess_repo(org: str, repo: str, output_dir: Path) -> AssessmentResult:
    """
    Clone repo, run agentready container, extract JSON, write to submissions dir.
    Always returns an AssessmentResult — never raises.
    """
    access = check_repo_access(org, repo)
    if access == "forbidden":
        return AssessmentResult(repo=repo, status="skipped", category="auth_forbidden",
                                message="HTTP 403 — token lacks access", output_path="")
    if access == "not_found":
        return AssessmentResult(repo=repo, status="skipped", category="auth_not_found",
                                message="HTTP 404 — repo not found or network error", output_path="")

    repo_submissions_dir = output_dir / org / repo
    repo_submissions_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"agentready-{repo}-") as tmp:
        clone_dir = Path(tmp) / "repo"
        output_tmp = Path(tmp) / "output"
        output_tmp.mkdir()

        # Shallow clone
        try:
            subprocess.run(
                [
                    "git", "clone", "--depth=1",
                    f"https://github.com/{org}/{repo}.git",
                    str(clone_dir),
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode(errors="replace")[:500]
            return AssessmentResult(repo=repo, status="failed", category="clone_failure",
                                    message=stderr, output_path="")
        except subprocess.TimeoutExpired:
            return AssessmentResult(repo=repo, status="failed", category="clone_failure",
                                    message="git clone timed out after 120s", output_path="")

        # Commit hash pre-check
        head_result = subprocess.run(
            ["git", "-C", str(clone_dir), "rev-parse", "HEAD"],
            capture_output=True,
            timeout=10,
        )
        if head_result.returncode != 0:
            return AssessmentResult(repo=repo, status="skipped", category="empty",
                                    message="repo has no commits", output_path="")
        head_hash = head_result.stdout.decode().strip()
        existing_latest = repo_submissions_dir / "assessment-latest.json"
        prior_hash = _prior_commit_hash(existing_latest)
        if prior_hash and head_hash == prior_hash:
            return AssessmentResult(repo=repo, status="skipped", category="unchanged",
                                    message=f"HEAD {head_hash[:8]} matches prior assessment",
                                    output_path="")

        uid = subprocess.check_output(["id", "-u"]).decode().strip()
        gid = subprocess.check_output(["id", "-g"]).decode().strip()

        # Run agentready container
        try:
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
                check=True,
                capture_output=True,
                timeout=600,
            )
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode(errors="replace")[:500]
            return AssessmentResult(repo=repo, status="failed", category="container_failure",
                                    message=stderr, output_path="")
        except subprocess.TimeoutExpired:
            return AssessmentResult(repo=repo, status="failed", category="container_failure",
                                    message="podman run timed out after 600s", output_path="")

        # Find timestamped assessment JSONs only (exclude symlinks)
        all_json = glob.glob(str(output_tmp / "assessment-*.json"))
        json_files = [f for f in all_json if not os.path.islink(f)]
        if not json_files:
            json_files = [str(Path(f).resolve()) for f in all_json if os.path.islink(f)]
        if not json_files:
            return AssessmentResult(repo=repo, status="failed", category="output_missing",
                                    message="no assessment JSON found in container output",
                                    output_path="")

        json_files.sort()
        src_json = Path(json_files[-1]).resolve()

        dest_json = repo_submissions_dir / src_json.name
        shutil.copy2(src_json, dest_json)

        symlink = repo_submissions_dir / "assessment-latest.json"
        if symlink.exists() or symlink.is_symlink():
            symlink.unlink()
        symlink.symlink_to(src_json.name)

        return AssessmentResult(repo=repo, status="succeeded", category="",
                                message="", output_path=str(dest_json))
```

- [ ] **Step 4: Update `run_batch()` to use `AssessmentResult` and return results list**

Replace the `run_batch` function (lines 279-327) with:

```python
def run_batch(
    org: str,
    repos: List[str],
    output_dir: Path,
    workers: int,
    retries: int,
) -> Tuple[List[str], List[str], List[str], List[AssessmentResult]]:
    """
    Run assessments concurrently.
    Returns (succeeded, failed, inaccessible, all_results).
    """
    succeeded = []
    inaccessible = []
    failed = list(repos)
    all_results: List[AssessmentResult] = []

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
                    all_results.append(result)
                    if result.status == "succeeded":
                        print(f"  ✓ {org}/{repo} → {result.output_path}")
                        succeeded.append(repo)
                    elif result.status == "skipped":
                        if result.category in ("auth_forbidden", "auth_not_found"):
                            print(f"  🔒 {org}/{repo} — {result.message}")
                            inaccessible.append(repo)
                        else:
                            print(f"  ⏭  {org}/{repo} — {result.category}: {result.message}")
                    else:
                        print(f"  ✗ {org}/{repo} — {result.category}: {result.message}")
                        failed.append(repo)
                except Exception as e:
                    print(f"  ✗ {org}/{repo}: {e}")
                    all_results.append(AssessmentResult(
                        repo=repo, status="failed", category="clone_failure",
                        message=str(e)[:500], output_path="",
                    ))
                    failed.append(repo)

    return succeeded, failed, inaccessible, all_results
```

- [ ] **Step 5: Update `TestRunBatchInaccessible` to work with new return type**

Replace the existing `TestRunBatchInaccessible` class in `tests/test_runner_lib.py`:

```python
class TestRunBatchInaccessible:
    def test_inaccessible_repos_separated(self, tmp_path):
        with patch("runner_lib.assess_repo") as mock_assess:
            def side_effect(org, repo, out):
                if repo == "accessible":
                    return AssessmentResult(repo=repo, status="succeeded", category="",
                                           message="", output_path=str(tmp_path / "result.json"))
                return AssessmentResult(repo=repo, status="skipped", category="auth_forbidden",
                                       message="HTTP 403", output_path="")

            mock_assess.side_effect = side_effect

            succeeded, failed, inaccessible, results = run_batch(
                org="myorg",
                repos=["accessible", "private"],
                output_dir=tmp_path,
                workers=1,
                retries=0,
            )

            assert succeeded == ["accessible"]
            assert failed == []
            assert inaccessible == ["private"]
            assert len(results) == 2

    def test_inaccessible_repos_not_retried(self, tmp_path):
        call_count = {"private": 0}

        def side_effect(org, repo, out):
            if repo == "private":
                call_count["private"] += 1
                return AssessmentResult(repo=repo, status="skipped", category="auth_forbidden",
                                       message="HTTP 403", output_path="")
            return AssessmentResult(repo=repo, status="succeeded", category="",
                                   message="", output_path=str(tmp_path / "result.json"))

        with patch("runner_lib.assess_repo", side_effect=side_effect):
            _, _, inaccessible, _ = run_batch(
                org="myorg",
                repos=["private"],
                output_dir=tmp_path,
                workers=1,
                retries=3,
            )

            assert inaccessible == ["private"]
            assert call_count["private"] == 1

    def test_failed_repos_retried(self, tmp_path):
        call_count = {"flaky": 0}

        def side_effect(org, repo, out):
            call_count["flaky"] += 1
            if call_count["flaky"] == 1:
                return AssessmentResult(repo=repo, status="failed", category="clone_failure",
                                       message="network blip", output_path="")
            return AssessmentResult(repo=repo, status="succeeded", category="",
                                   message="", output_path=str(tmp_path / "result.json"))

        with patch("runner_lib.assess_repo", side_effect=side_effect):
            succeeded, failed, _, _ = run_batch(
                org="myorg",
                repos=["flaky"],
                output_dir=tmp_path,
                workers=1,
                retries=1,
            )

            assert succeeded == ["flaky"]
            assert failed == []
            assert call_count["flaky"] == 2
```

- [ ] **Step 6: Update `assess.py` to unpack the 4th element from `run_batch`**

In `runner/assess.py`, update line 92 in `process_org()` to accept the 4th return value and return it:

```python
    succeeded, failed, inaccessible, results = run_batch(
        org=org,
        repos=repos,
        output_dir=args.output_dir,
        workers=args.workers,
        retries=args.retries,
    )
```

Update `process_org()` return statement (line 117) to also return results:

```python
    return len(succeeded), len(failed), len(inaccessible), results
```

Update `main()` call sites (lines 153, 165) to unpack the 4th element:

```python
            s, f, i, _ = process_org(org, repos, exclusions, args, runner_lib)
```

And at line 165:

```python
        s, f, i, _ = process_org(org, repos, exclusions, args, runner_lib)
```

Also add `AssessmentResult` to the imports from `runner_lib` (line 124-132).

- [ ] **Step 7: Run all tests**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 8: Commit**

```bash
git add runner/runner_lib.py runner/assess.py tests/test_runner_lib.py
git commit -m "feat(runner): assess_repo returns AssessmentResult, run_batch returns results list"
```

---

### Task 3: Add `write_error_details()`, `load_error_details()`, and wire into `assess.py`

**Files:**
- Modify: `runner/runner_lib.py` (append two functions)
- Modify: `runner/assess.py` (call `write_error_details` in `process_org`)
- Modify: `tests/test_runner_lib.py` (add test classes)

**Interfaces:**
- Consumes: `AssessmentResult` dataclass (from Task 1)
- Produces:
  - `write_error_details(path: Path, org: str, results: list[AssessmentResult]) -> None`
  - `load_error_details(runner_dir: Path) -> list[dict]` — each dict has keys `org`, `repo`, `status`, `category`, `message`

- [ ] **Step 1: Write failing tests for `write_error_details` and `load_error_details`**

Add to `tests/test_runner_lib.py`:

```python
import json

from runner_lib import write_error_details, load_error_details


class TestWriteErrorDetails:
    def test_writes_only_non_succeeded(self, tmp_path):
        results = [
            AssessmentResult(repo="good", status="succeeded", category="", message="", output_path="/tmp/x.json"),
            AssessmentResult(repo="bad", status="failed", category="clone_failure", message="fatal: not found", output_path=""),
            AssessmentResult(repo="skip", status="skipped", category="empty", message="no commits", output_path=""),
        ]
        out = tmp_path / "errors-testorg.json"
        write_error_details(out, "testorg", results)
        data = json.loads(out.read_text())
        assert data["org"] == "testorg"
        assert "timestamp" in data
        assert len(data["errors"]) == 2
        repos = [e["repo"] for e in data["errors"]]
        assert "bad" in repos
        assert "skip" in repos
        assert "good" not in repos

    def test_error_entry_fields(self, tmp_path):
        results = [
            AssessmentResult(repo="bad", status="failed", category="container_failure",
                             message="OOM killed", output_path=""),
        ]
        out = tmp_path / "errors-org.json"
        write_error_details(out, "org", results)
        data = json.loads(out.read_text())
        entry = data["errors"][0]
        assert entry["repo"] == "bad"
        assert entry["status"] == "failed"
        assert entry["category"] == "container_failure"
        assert entry["message"] == "OOM killed"

    def test_all_succeeded_writes_empty_errors(self, tmp_path):
        results = [
            AssessmentResult(repo="good", status="succeeded", category="", message="", output_path="/tmp/x.json"),
        ]
        out = tmp_path / "errors-org.json"
        write_error_details(out, "org", results)
        data = json.loads(out.read_text())
        assert data["errors"] == []

    def test_empty_results_writes_empty_errors(self, tmp_path):
        out = tmp_path / "errors-org.json"
        write_error_details(out, "org", [])
        data = json.loads(out.read_text())
        assert data["errors"] == []


class TestLoadErrorDetails:
    def test_reads_multiple_files(self, tmp_path):
        (tmp_path / "errors-orgA.json").write_text(json.dumps({
            "org": "orgA", "timestamp": "2026-01-01T00:00:00Z",
            "errors": [{"repo": "r1", "status": "failed", "category": "clone_failure", "message": "err"}],
        }))
        (tmp_path / "errors-orgB.json").write_text(json.dumps({
            "org": "orgB", "timestamp": "2026-01-01T00:00:00Z",
            "errors": [{"repo": "r2", "status": "skipped", "category": "empty", "message": "no commits"}],
        }))
        entries = load_error_details(tmp_path)
        assert len(entries) == 2
        orgs = {e["org"] for e in entries}
        assert orgs == {"orgA", "orgB"}

    def test_no_files_returns_empty(self, tmp_path):
        entries = load_error_details(tmp_path)
        assert entries == []

    def test_each_entry_includes_org(self, tmp_path):
        (tmp_path / "errors-myorg.json").write_text(json.dumps({
            "org": "myorg", "timestamp": "2026-01-01T00:00:00Z",
            "errors": [{"repo": "r1", "status": "failed", "category": "clone_failure", "message": "err"}],
        }))
        entries = load_error_details(tmp_path)
        assert entries[0]["org"] == "myorg"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_runner_lib.py::TestWriteErrorDetails tests/test_runner_lib.py::TestLoadErrorDetails -v`
Expected: FAIL — `ImportError: cannot import name 'write_error_details'`

- [ ] **Step 3: Implement `write_error_details()` and `load_error_details()`**

Append to `runner/runner_lib.py`:

```python
def write_error_details(path: Path, org: str, results: list) -> None:
    """Write error details JSON for non-succeeded assessment results."""
    errors = [
        {
            "repo": r.repo,
            "status": r.status,
            "category": r.category,
            "message": r.message,
        }
        for r in results
        if r.status != "succeeded"
    ]
    data = {
        "org": org,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "errors": errors,
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_error_details(runner_dir: Path) -> list:
    """Read all errors-*.json files and return a flat list of error entries."""
    entries = []
    for path in sorted(runner_dir.glob("errors-*.json")):
        with open(path) as f:
            data = json.load(f)
        org = data.get("org", "?")
        for entry in data.get("errors", []):
            entry["org"] = org
            entries.append(entry)
    return entries
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_runner_lib.py::TestWriteErrorDetails tests/test_runner_lib.py::TestLoadErrorDetails -v`
Expected: 7 PASSED

- [ ] **Step 5: Wire `write_error_details()` into `assess.py`**

In `runner/assess.py`, add `write_error_details` to the imports from `runner_lib` (line 124-132):

```python
    from runner_lib import (
        AssessmentResult,
        SchemaError,
        load_repos_from_file,
        load_exclusions,
        discover_org_repos,
        run_batch,
        commit_results,
        write_failed_repos,
        write_error_details,
    )
```

Add `"write_error_details": write_error_details,` to the `runner_lib` dict (line 134-140).

In `process_org()`, add `write_error_details` extraction and call after the `write_failed_repos` block. Update the function to accept it from the dict and call it:

Add after line 72:
```python
    write_error_details_fn = runner_lib["write_error_details"]
```

Update the `process_org` return type — change line 92 to unpack 4 values:
```python
    succeeded, failed, inaccessible, results = run_batch(...)
```

Add after the inaccessible block (after line 115):
```python
    errors_path = SCRIPT_DIR / f"errors-{org}.json"
    write_error_details_fn(errors_path, org, results)
```

Update return to include results:
```python
    return len(succeeded), len(failed), len(inaccessible), results
```

- [ ] **Step 6: Run all tests**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 7: Commit**

```bash
git add runner/runner_lib.py runner/assess.py tests/test_runner_lib.py
git commit -m "feat(runner): add write_error_details/load_error_details, wire into assess.py"
```

---

### Task 4: Add `emit_gha_annotations()` to `collect_summary.py` with tests

**Files:**
- Modify: `runner/collect_summary.py` (add function, call in `main()`)
- Modify: `tests/test_collect_summary.py` (add test class)

**Interfaces:**
- Consumes: `load_error_details(runner_dir: Path) -> list[dict]` (from Task 3, in `runner_lib.py`)
- Produces: `emit_gha_annotations(runner_dir: Path) -> None` — prints `::error`/`::warning` lines to stdout

- [ ] **Step 1: Write failing tests for `emit_gha_annotations`**

Add to `tests/test_collect_summary.py`:

```python
import json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from collect_summary import emit_gha_annotations


class TestEmitGhaAnnotations:
    def _write_errors(self, path, org, errors):
        data = {"org": org, "timestamp": "2026-01-01T00:00:00Z", "errors": errors}
        with open(path, "w") as f:
            json.dump(data, f)

    def test_failure_emits_error(self, tmp_path, capsys):
        self._write_errors(tmp_path / "errors-myorg.json", "myorg", [
            {"repo": "bad-repo", "status": "failed", "category": "clone_failure",
             "message": "fatal: repository not found"},
        ])
        emit_gha_annotations(tmp_path)
        out = capsys.readouterr().out
        assert "::error title=clone_failure::myorg/bad-repo: fatal: repository not found" in out

    def test_auth_forbidden_emits_warning(self, tmp_path, capsys):
        self._write_errors(tmp_path / "errors-myorg.json", "myorg", [
            {"repo": "secret-repo", "status": "skipped", "category": "auth_forbidden",
             "message": "HTTP 403 — token lacks access"},
        ])
        emit_gha_annotations(tmp_path)
        out = capsys.readouterr().out
        assert "::warning title=auth_forbidden::myorg/secret-repo: HTTP 403" in out

    def test_auth_not_found_emits_warning(self, tmp_path, capsys):
        self._write_errors(tmp_path / "errors-myorg.json", "myorg", [
            {"repo": "gone-repo", "status": "skipped", "category": "auth_not_found",
             "message": "HTTP 404"},
        ])
        emit_gha_annotations(tmp_path)
        out = capsys.readouterr().out
        assert "::warning title=auth_not_found::" in out

    def test_empty_skip_no_annotation(self, tmp_path, capsys):
        self._write_errors(tmp_path / "errors-myorg.json", "myorg", [
            {"repo": "empty-repo", "status": "skipped", "category": "empty",
             "message": "repo has no commits"},
        ])
        emit_gha_annotations(tmp_path)
        out = capsys.readouterr().out
        assert "::error" not in out
        assert "::warning" not in out

    def test_unchanged_skip_no_annotation(self, tmp_path, capsys):
        self._write_errors(tmp_path / "errors-myorg.json", "myorg", [
            {"repo": "stable-repo", "status": "skipped", "category": "unchanged",
             "message": "HEAD matches prior"},
        ])
        emit_gha_annotations(tmp_path)
        out = capsys.readouterr().out
        assert "::error" not in out
        assert "::warning" not in out

    def test_no_error_files_is_noop(self, tmp_path, capsys):
        emit_gha_annotations(tmp_path)
        out = capsys.readouterr().out
        assert out == ""

    def test_multiple_orgs(self, tmp_path, capsys):
        self._write_errors(tmp_path / "errors-orgA.json", "orgA", [
            {"repo": "r1", "status": "failed", "category": "clone_failure", "message": "err1"},
        ])
        self._write_errors(tmp_path / "errors-orgB.json", "orgB", [
            {"repo": "r2", "status": "failed", "category": "container_failure", "message": "err2"},
        ])
        emit_gha_annotations(tmp_path)
        out = capsys.readouterr().out
        assert "orgA/r1" in out
        assert "orgB/r2" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_collect_summary.py::TestEmitGhaAnnotations -v`
Expected: FAIL — `ImportError: cannot import name 'emit_gha_annotations'`

- [ ] **Step 3: Implement `emit_gha_annotations()` in `collect_summary.py`**

Add to `runner/collect_summary.py`, after the existing imports, add the import for `load_error_details`:

```python
from runner_lib import validate_token, collect_summary, load_error_details
```

Add the function before `main()`:

```python
def emit_gha_annotations(runner_dir: Path) -> None:
    """Read errors-*.json and emit ::error/::warning GHA annotations."""
    entries = load_error_details(runner_dir)
    for entry in entries:
        org = entry.get("org", "?")
        repo = entry.get("repo", "?")
        status = entry.get("status", "")
        category = entry.get("category", "")
        message = entry.get("message", "")

        if status == "failed":
            print(f"::error title={category}::{org}/{repo}: {message}")
        elif category in ("auth_forbidden", "auth_not_found"):
            print(f"::warning title={category}::{org}/{repo}: {message}")
        # empty and unchanged: no annotation
```

- [ ] **Step 4: Call `emit_gha_annotations()` in `main()`**

In the `main()` function of `runner/collect_summary.py`, add the call after `collect_summary()` (after line 41):

```python
    summary = collect_summary(args.runner_dir)
    emit_gha_annotations(args.runner_dir)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_collect_summary.py::TestEmitGhaAnnotations -v`
Expected: 7 PASSED

- [ ] **Step 6: Run all tests**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 7: Commit**

```bash
git add runner/collect_summary.py tests/test_collect_summary.py
git commit -m "feat(runner): add GHA annotations for assessment failures"
```

---

### Task 5: Add e2e test scaffolding with direnv

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/test_e2e.py`
- Create: `env.example`

**Interfaces:**
- Consumes: `assess_repo(org, repo, output_dir) -> AssessmentResult` (from Task 2)
- Produces: e2e tests runnable with `uv run pytest tests/test_e2e.py --run-e2e -v`

**Secrets:** Managed via direnv — developers create `.envrc` with `export GH_TOKEN=...`, run `direnv allow`, and env vars are available to all commands in the directory including `uv run pytest`. No Python-level dotenv loading needed.

- [ ] **Step 1: Create `env.example`**

Create `env.example` in the repo root (documents required vars; developers copy to `.envrc`):

```bash
# Copy to .envrc and fill in real values, then run: direnv allow
export GH_TOKEN=ghp_your_token_here
```

- [ ] **Step 2: Ensure `.envrc` is gitignored**

Check `.gitignore` — it already has `.env`. Add `.envrc` if not present:

```
.envrc
```

- [ ] **Step 3: Create `tests/conftest.py` with `--run-e2e` option**

```python
import pytest


def pytest_addoption(parser):
    parser.addoption("--run-e2e", action="store_true", default=False,
                     help="Run e2e tests (requires GH_TOKEN env var and podman)")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-e2e"):
        skip = pytest.mark.skip(reason="needs --run-e2e")
        for item in items:
            if "e2e" in item.keywords:
                item.add_marker(skip)
```

- [ ] **Step 4: Create `tests/test_e2e.py` with full pipeline tests**

```python
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from runner_lib import AssessmentResult, assess_repo


@pytest.mark.e2e
class TestE2EPipeline:
    """Full pipeline tests — require podman, GH_TOKEN in env, and --run-e2e flag."""

    @pytest.fixture(autouse=True)
    def _require_token(self):
        if not os.environ.get("GH_TOKEN"):
            pytest.skip("GH_TOKEN not set — run direnv allow or export it manually")

    def test_successful_assessment(self, tmp_path):
        """Assess a known small public repo end-to-end."""
        result = assess_repo("redhat-community-ai-tools", "agentready", tmp_path)
        assert isinstance(result, AssessmentResult)
        assert result.status == "succeeded", f"Expected succeeded, got {result.status}: {result.message}"
        assert result.output_path != ""
        assert Path(result.output_path).exists()
        latest = tmp_path / "redhat-community-ai-tools" / "agentready" / "assessment-latest.json"
        assert latest.is_symlink()

    def test_nonexistent_repo(self, tmp_path):
        """A repo that doesn't exist should be skipped with auth_not_found."""
        result = assess_repo("redhat-community-ai-tools", "this-repo-does-not-exist-xyz-999", tmp_path)
        assert isinstance(result, AssessmentResult)
        assert result.status == "skipped"
        assert result.category in ("auth_not_found", "auth_forbidden")
```

- [ ] **Step 5: Run unit tests to verify nothing is broken**

Run: `uv run pytest tests/ -v`
Expected: All existing tests pass, e2e tests SKIPPED (no `--run-e2e`)

- [ ] **Step 6: Run e2e tests (requires direnv-loaded `GH_TOKEN` and podman)**

Run: `uv run pytest tests/test_e2e.py --run-e2e -v`
Expected: 2 PASSED (may take 1-3 minutes due to container execution). If `GH_TOKEN` is not set, tests skip with a clear message.

- [ ] **Step 7: Commit**

```bash
git add .gitignore env.example tests/conftest.py tests/test_e2e.py
git commit -m "feat(tests): add e2e test scaffolding with direnv and podman"
```

---

### Summary of changes

| Before | After |
|---|---|
| `assess_repo()` returns `str` or raises | Returns `AssessmentResult` — never raises |
| `check_repo_access()` returns `bool` | Returns `"accessible"`, `"forbidden"`, or `"not_found"` |
| `run_batch()` returns 3-tuple | Returns 4-tuple with `List[AssessmentResult]` |
| stderr from git/podman silently dropped | Captured in `AssessmentResult.message` (max 500 chars) |
| All failures generic "failed" in YAML | Categorized: `clone_failure`, `container_failure`, `output_missing` |
| Auth issues just "inaccessible" | Distinguish `auth_forbidden` (403) vs `auth_not_found` (404) |
| No CI diagnostics | `::error`/`::warning` GHA annotations in run summary |
| No e2e tests | Full pipeline e2e tests with podman + direnv secrets |
