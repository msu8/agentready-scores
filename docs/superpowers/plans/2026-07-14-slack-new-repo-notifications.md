# Slack Notification for Newly-Scored Repositories Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Notify Slack (batched, once per scheduled run) and emit GitHub Actions info annotations when a repository gets its first-ever successful score.

**Architecture:** `assess_repo()` already checks whether `assessment-latest.json` existed before this run to decide if a repo is unchanged — that check is extended to also record `is_new`/`score` on `AssessmentResult`. `process_org()` writes newly-scored repos to `runner/status/new-<org>.yaml`, mirroring the existing `failed-<org>.yaml` pattern. `collect_summary.py` aggregates those files into GH Action outputs and `::notice::` annotations. `assess-scheduled.yml` gains one job that turns the aggregated output into a single Slack message, reusing the existing `SLACK_WEBHOOK_URL`/`SLACK_NOTIFICATIONS` configuration.

**Tech Stack:** Python 3.11, pytest, PyYAML, GitHub Actions, `slackapi/slack-github-action@v2`

**Design spec:** `docs/superpowers/specs/2026-07-14-slack-new-repo-notifications-design.md`

## Global Constraints

- Python 3.11+ (workflow uses `setup-python` with `3.11`)
- Dependencies limited to `runner/requirements.txt`
- Tests run locally with `uv run pytest tests/ -v` (per `justfile`)
- `failed-*.yaml`/`inaccessible-*.yaml`/`errors-*.json` formats are unchanged
- `new-<org>.yaml` is committed (mirrors `failed-<org>.yaml`), unlike `errors-*.json` which stays transient
- `.github/workflows/assess-manual.yml` is NOT modified — the new Slack job goes only in `assess-scheduled.yml`
- No new secrets or repository variables — reuses `SLACK_WEBHOOK_URL` / `SLACK_NOTIFICATIONS`
- `AssessmentResult.is_new`/`score` must default such that every existing keyword-argument call site keeps working unchanged

---

### Task 1: Detect newly-scored repos in `assess_repo()`

**Files:**
- Modify: `runner/runner_lib.py:22-28` (dataclass), `runner/runner_lib.py:186-195` (add `_read_overall_score` after `_prior_commit_hash`), `runner/runner_lib.py:260-266` (compute `is_new`), `runner/runner_lib.py:314-323` (compute `score`, pass both to the succeeded return)
- Test: `tests/test_runner_lib.py`

**Interfaces:**
- Consumes: nothing from other tasks
- Produces:
  - `AssessmentResult` gains `is_new: bool = False`, `score: Optional[float] = None`
  - `_read_overall_score(json_path: Path) -> Optional[float]` (private helper, same style as `_prior_commit_hash`)

- [ ] **Step 1: Write failing tests for `is_new`/`score` detection**

Add to `tests/test_runner_lib.py`, inside the existing `TestAssessRepo` class (after `test_container_failure`, before the closing of the class):

```python
    def test_new_repo_sets_is_new_and_score(self, tmp_path):
        # No pre-existing assessment-latest.json for this repo — first time ever assessed.
        call_count = {"n": 0}

        def mock_run(cmd, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return MagicMock(returncode=0)  # git clone
            if call_count["n"] == 2:
                m = MagicMock()
                m.returncode = 0
                m.stdout = b"abc123\n"
                return m  # git rev-parse HEAD
            # podman run — write a fake assessment JSON into the mounted output dir
            output_dir = None
            for arg in cmd:
                if isinstance(arg, str) and arg.endswith(":/reports:z"):
                    output_dir = Path(arg.split(":")[0])
            assert output_dir is not None, "could not find /reports volume mount in podman command"
            (output_dir / "assessment-20260101-000000.json").write_text(
                json.dumps({"overall_score": 42.5, "repository": {"commit_hash": "abc123"}})
            )
            return MagicMock(returncode=0)

        with patch("runner_lib.check_repo_access", return_value="accessible"), \
             patch("runner_lib.subprocess.run", side_effect=mock_run), \
             patch("runner_lib.subprocess.check_output", return_value=b"1000\n"):
            result = assess_repo("myorg", "new-repo", tmp_path)

        assert result.status == "succeeded"
        assert result.is_new is True
        assert result.score == 42.5

    def test_existing_repo_new_commit_is_not_new(self, tmp_path):
        repo_dir = tmp_path / "myorg" / "existing-repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "assessment-latest.json").write_text(
            json.dumps({"overall_score": 10.0, "repository": {"commit_hash": "oldsha"}})
        )

        call_count = {"n": 0}

        def mock_run(cmd, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return MagicMock(returncode=0)
            if call_count["n"] == 2:
                m = MagicMock()
                m.returncode = 0
                m.stdout = b"newsha\n"
                return m
            output_dir = None
            for arg in cmd:
                if isinstance(arg, str) and arg.endswith(":/reports:z"):
                    output_dir = Path(arg.split(":")[0])
            assert output_dir is not None
            (output_dir / "assessment-20260101-000000.json").write_text(
                json.dumps({"overall_score": 55.0, "repository": {"commit_hash": "newsha"}})
            )
            return MagicMock(returncode=0)

        with patch("runner_lib.check_repo_access", return_value="accessible"), \
             patch("runner_lib.subprocess.run", side_effect=mock_run), \
             patch("runner_lib.subprocess.check_output", return_value=b"1000\n"):
            result = assess_repo("myorg", "existing-repo", tmp_path)

        assert result.status == "succeeded"
        assert result.is_new is False
        assert result.score == 55.0

    def test_corrupted_existing_file_is_not_new(self, tmp_path):
        # File exists but fails to parse — must NOT be reported as new, even though
        # _prior_commit_hash() returns None for it (same as a genuinely missing file).
        repo_dir = tmp_path / "myorg" / "flaky-repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "assessment-latest.json").write_text("not valid json{")

        call_count = {"n": 0}

        def mock_run(cmd, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return MagicMock(returncode=0)
            if call_count["n"] == 2:
                m = MagicMock()
                m.returncode = 0
                m.stdout = b"newsha\n"
                return m
            output_dir = None
            for arg in cmd:
                if isinstance(arg, str) and arg.endswith(":/reports:z"):
                    output_dir = Path(arg.split(":")[0])
            assert output_dir is not None
            (output_dir / "assessment-20260101-000000.json").write_text(
                json.dumps({"overall_score": 30.0, "repository": {"commit_hash": "newsha"}})
            )
            return MagicMock(returncode=0)

        with patch("runner_lib.check_repo_access", return_value="accessible"), \
             patch("runner_lib.subprocess.run", side_effect=mock_run), \
             patch("runner_lib.subprocess.check_output", return_value=b"1000\n"):
            result = assess_repo("myorg", "flaky-repo", tmp_path)

        assert result.status == "succeeded"
        assert result.is_new is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_runner_lib.py::TestAssessRepo -v -k "new_repo or existing_repo or corrupted"`
Expected: FAIL — `AttributeError: 'AssessmentResult' object has no attribute 'is_new'`

- [ ] **Step 3: Add `is_new`/`score` fields to `AssessmentResult`**

Replace `runner/runner_lib.py:22-28`:

```python
@dataclasses.dataclass
class AssessmentResult:
    repo: str
    status: str
    category: str
    message: str
    output_path: str
    is_new: bool = False
    score: Optional[float] = None
```

- [ ] **Step 4: Add `_read_overall_score()` helper**

In `runner/runner_lib.py`, immediately after `_prior_commit_hash()` (after line 195), add:

```python
def _read_overall_score(json_path: Path) -> Optional[float]:
    """Return overall_score from an assessment JSON file, or None if unreadable."""
    try:
        with open(json_path) as f:
            return json.load(f).get("overall_score")
    except Exception:
        return None
```

- [ ] **Step 5: Compute `is_new` before the unchanged-check, and `score` on the succeeded return**

In `assess_repo()`, replace `runner/runner_lib.py:260-266`:

```python
        head_hash = head_result.stdout.decode().strip()
        existing_latest = repo_submissions_dir / "assessment-latest.json"
        prior_hash = _prior_commit_hash(existing_latest)
        if prior_hash and head_hash == prior_hash:
            return AssessmentResult(repo=repo, status="skipped", category="unchanged",
                                    message=f"HEAD {head_hash[:8]} matches prior assessment",
                                    output_path="")
```

with:

```python
        head_hash = head_result.stdout.decode().strip()
        existing_latest = repo_submissions_dir / "assessment-latest.json"
        # Computed via existence, not via prior_hash being None — a corrupted or
        # unparseable existing file must not be misreported as "new".
        is_new = not existing_latest.exists()
        prior_hash = _prior_commit_hash(existing_latest)
        if prior_hash and head_hash == prior_hash:
            return AssessmentResult(repo=repo, status="skipped", category="unchanged",
                                    message=f"HEAD {head_hash[:8]} matches prior assessment",
                                    output_path="")
```

Then replace the succeeded return at `runner/runner_lib.py:314-323`:

```python
        dest_json = repo_submissions_dir / src_json.name
        shutil.copy2(src_json, dest_json)

        symlink = repo_submissions_dir / "assessment-latest.json"
        if symlink.exists() or symlink.is_symlink():
            symlink.unlink()
        symlink.symlink_to(src_json.name)

        return AssessmentResult(repo=repo, status="succeeded", category="",
                                message="", output_path=str(dest_json))
```

with:

```python
        dest_json = repo_submissions_dir / src_json.name
        shutil.copy2(src_json, dest_json)
        score = _read_overall_score(dest_json)

        symlink = repo_submissions_dir / "assessment-latest.json"
        if symlink.exists() or symlink.is_symlink():
            symlink.unlink()
        symlink.symlink_to(src_json.name)

        return AssessmentResult(repo=repo, status="succeeded", category="",
                                message="", output_path=str(dest_json),
                                is_new=is_new, score=score)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_runner_lib.py -v`
Expected: all tests PASS (existing tests unaffected by the new default-valued fields)

- [ ] **Step 7: Commit**

```bash
git add runner/runner_lib.py tests/test_runner_lib.py
git commit -m "feat: detect newly-scored repos in assess_repo()

AssessmentResult gains is_new/score. is_new is computed from
assessment-latest.json existence (not prior-hash-is-None), so a
corrupted history file is never misreported as a new repo."
```

---

### Task 2: Write and load per-org new-repo status files

**Files:**
- Modify: `runner/runner_lib.py` (add `write_new_repos()` after `write_failed_repos()` at line 421, add `load_new_repos()` after `load_error_details()` at end of file)
- Modify: `runner/assess.py:71-124` (`process_org()` — write `new-<org>.yaml`), `runner/assess.py` `main()` (wire `write_new_repos` into the `runner_lib` dict and import)
- Test: `tests/test_runner_lib.py`

**Interfaces:**
- Consumes: `AssessmentResult.is_new`/`score` from Task 1
- Produces:
  - `write_new_repos(path: Path, org: str, repos: List[Tuple[str, Optional[float]]]) -> None`
  - `load_new_repos(runner_dir: Path) -> list[dict]` — each entry `{"org": str, "name": str, "score": Optional[float]}`

- [ ] **Step 1: Write failing tests for `write_new_repos()` and `load_new_repos()`**

Add to `tests/test_runner_lib.py`. First add `import yaml` to the top of the file (it isn't imported there yet) and extend the `from runner_lib import (...)` line:

```python
import json
import subprocess
import sys
import yaml
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from runner_lib import (
    AssessmentResult, assess_repo, check_repo_access, run_batch,
    write_error_details, load_error_details, write_new_repos, load_new_repos,
)
```

Then add two new test classes at the end of the file:

```python
class TestWriteNewRepos:
    def test_writes_expected_shape(self, tmp_path):
        path = tmp_path / "new-myorg.yaml"
        write_new_repos(path, "myorg", [("repo-a", 42.5), ("repo-b", 18.0)])
        with open(path) as f:
            data = yaml.safe_load(f)
        assert data["org"] == "myorg"
        assert data["repos"] == [
            {"name": "repo-a", "score": 42.5},
            {"name": "repo-b", "score": 18.0},
        ]

    def test_handles_missing_score(self, tmp_path):
        path = tmp_path / "new-myorg.yaml"
        write_new_repos(path, "myorg", [("repo-a", None)])
        with open(path) as f:
            data = yaml.safe_load(f)
        assert data["repos"] == [{"name": "repo-a", "score": None}]


class TestLoadNewRepos:
    def test_reads_multiple_files(self, tmp_path):
        write_new_repos(tmp_path / "new-orgA.yaml", "orgA", [("r1", 10.0)])
        write_new_repos(tmp_path / "new-orgB.yaml", "orgB", [("r2", 20.0)])
        entries = load_new_repos(tmp_path)
        assert {"org": "orgA", "name": "r1", "score": 10.0} in entries
        assert {"org": "orgB", "name": "r2", "score": 20.0} in entries

    def test_no_files_returns_empty(self, tmp_path):
        assert load_new_repos(tmp_path) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_runner_lib.py::TestWriteNewRepos tests/test_runner_lib.py::TestLoadNewRepos -v`
Expected: FAIL — `ImportError: cannot import name 'write_new_repos'`

- [ ] **Step 3: Add `write_new_repos()` to `runner_lib.py`**

Immediately after `write_failed_repos()` (after line 421), add:

```python
def write_new_repos(path: Path, org: str, repos: List[Tuple[str, Optional[float]]]) -> None:
    """Write newly-scored repos (name + score) to a YAML file."""
    data = {"org": org, "repos": [{"name": name, "score": score} for name, score in repos]}
    with open(path, "w") as f:
        f.write(f"# New repos scored from {datetime.now(timezone.utc).isoformat()}\n")
        yaml.dump(data, f, default_flow_style=False)
```

- [ ] **Step 4: Add `load_new_repos()` to `runner_lib.py`**

Immediately after `load_error_details()` (at the end of the file), add:

```python
def load_new_repos(runner_dir: Path) -> list:
    """Read all new-*.yaml files and return a flat list of {org, name, score} entries."""
    entries = []
    for path in sorted(runner_dir.glob("new-*.yaml")):
        with open(path) as f:
            data = yaml.safe_load(f)
        org = data.get("org", "?")
        for repo in data.get("repos") or []:
            entries.append({"org": org, "name": repo.get("name", "?"), "score": repo.get("score")})
    return entries
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_runner_lib.py -v`
Expected: all tests PASS

- [ ] **Step 6: Wire `write_new_repos()` into `process_org()`**

In `runner/assess.py`, replace the `process_org()` header (lines 71-78):

```python
def process_org(org, repos, exclusions, args, runner_lib):
    """Run assessments for one org, commit results, write failures."""
    load_exclusions = runner_lib["load_exclusions"]
    discover_org_repos = runner_lib["discover_org_repos"]
    run_batch = runner_lib["run_batch"]
    commit_results = runner_lib["commit_results"]
    write_failed_repos = runner_lib["write_failed_repos"]
    write_error_details_fn = runner_lib["write_error_details"]
```

with:

```python
def process_org(org, repos, exclusions, args, runner_lib):
    """Run assessments for one org, commit results, write failures."""
    load_exclusions = runner_lib["load_exclusions"]
    discover_org_repos = runner_lib["discover_org_repos"]
    run_batch = runner_lib["run_batch"]
    commit_results = runner_lib["commit_results"]
    write_failed_repos = runner_lib["write_failed_repos"]
    write_error_details_fn = runner_lib["write_error_details"]
    write_new_repos_fn = runner_lib["write_new_repos"]
```

Then, in the same function, replace the inaccessible-file block (lines 114-119):

```python
    inaccessible_path = STATUS_DIR / f"inaccessible-{org}.yaml"
    if inaccessible:
        write_failed_repos(inaccessible_path, org, inaccessible)
        print(f"{len(inaccessible)} repos inaccessible. Written to {inaccessible_path}")
    elif inaccessible_path.exists():
        inaccessible_path.unlink()
```

with:

```python
    inaccessible_path = STATUS_DIR / f"inaccessible-{org}.yaml"
    if inaccessible:
        write_failed_repos(inaccessible_path, org, inaccessible)
        print(f"{len(inaccessible)} repos inaccessible. Written to {inaccessible_path}")
    elif inaccessible_path.exists():
        inaccessible_path.unlink()

    new_scored = [(r.repo, r.score) for r in results if r.status == "succeeded" and r.is_new]
    new_path = STATUS_DIR / f"new-{org}.yaml"
    if new_scored:
        write_new_repos_fn(new_path, org, new_scored)
        print(f"{len(new_scored)} new repo(s) scored. Written to {new_path}")
    elif new_path.exists():
        new_path.unlink()
```

- [ ] **Step 7: Import and register `write_new_repos` in `main()`**

In `runner/assess.py`, inside `main()`, replace the `runner_lib` import block:

```python
    from runner_lib import (
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

with:

```python
    from runner_lib import (
        SchemaError,
        load_repos_from_file,
        load_exclusions,
        discover_org_repos,
        run_batch,
        commit_results,
        write_failed_repos,
        write_error_details,
        write_new_repos,
    )
```

Then replace the `runner_lib = {...}` dict immediately below it:

```python
    runner_lib = {
        "load_exclusions": load_exclusions,
        "discover_org_repos": discover_org_repos,
        "run_batch": run_batch,
        "commit_results": commit_results,
        "write_failed_repos": write_failed_repos,
        "write_error_details": write_error_details,
    }
```

with:

```python
    runner_lib = {
        "load_exclusions": load_exclusions,
        "discover_org_repos": discover_org_repos,
        "run_batch": run_batch,
        "commit_results": commit_results,
        "write_failed_repos": write_failed_repos,
        "write_error_details": write_error_details,
        "write_new_repos": write_new_repos,
    }
```

- [ ] **Step 8: Sanity-check `assess.py` still imports cleanly**

Run: `python3 -c "import sys; sys.path.insert(0, 'runner'); import assess"`
Expected: no output, exit code 0 (no `ImportError`/`NameError`)

- [ ] **Step 9: Commit**

```bash
git add runner/runner_lib.py runner/assess.py tests/test_runner_lib.py
git commit -m "feat: write new-<org>.yaml for newly-scored repos

Mirrors write_failed_repos()/failed-<org>.yaml. process_org() writes
one entry per repo that succeeded with is_new=True this run, and
deletes a stale file when there are none."
```

---

### Task 3: Aggregate new repos into GH Action outputs and annotations

**Files:**
- Modify: `runner/runner_lib.py:448-480` (`collect_summary()`)
- Modify: `runner/collect_summary.py` (`emit_gha_annotations()`, `main()`)
- Test: `tests/test_collect_summary.py`

**Interfaces:**
- Consumes: `load_new_repos()` from Task 2
- Produces: `collect_summary()` return dict gains `has_new_repos: bool`, `new_repos_count: int`, `new_repos: str`. `$GITHUB_OUTPUT` gains `has_new_repos`, `new_repos_count`, `new_repos`. `emit_gha_annotations()` additionally prints one `::notice title=new_repo::...` line per new repo.

- [ ] **Step 1: Write failing tests**

In `tests/test_collect_summary.py`, extend the import line:

```python
from runner_lib import validate_token, collect_summary, write_new_repos
from collect_summary import emit_gha_annotations
```

Extend the existing `test_no_files` method in `TestCollectSummary` (it already asserts the failure-side zero state — add the new-repo zero state to the same method rather than duplicating a fixture-less test):

```python
    def test_no_files(self, tmp_path):
        result = collect_summary(tmp_path)
        assert result["has_failures"] is False
        assert result["failed_count"] == 0
        assert result["failed_repos"] == ""
        assert result["inaccessible_count"] == 0
        assert result["has_new_repos"] is False
        assert result["new_repos_count"] == 0
        assert result["new_repos"] == ""
```

Add new test methods to `TestCollectSummary` (after `test_empty_repos_list`):

```python
    def test_new_repos_only(self, tmp_path):
        write_new_repos(tmp_path / "new-myorg.yaml", "myorg", [("repo-a", 42.5)])
        result = collect_summary(tmp_path)
        assert result["has_new_repos"] is True
        assert result["new_repos_count"] == 1
        assert "myorg/repo-a (42.5)" in result["new_repos"]

    def test_new_repos_multiple_orgs(self, tmp_path):
        write_new_repos(tmp_path / "new-orgA.yaml", "orgA", [("r1", 10.0)])
        write_new_repos(tmp_path / "new-orgB.yaml", "orgB", [("r2", 20.0)])
        result = collect_summary(tmp_path)
        assert result["new_repos_count"] == 2

    def test_new_repos_truncated_at_20(self, tmp_path):
        repos = [(f"repo-{i}", float(i)) for i in range(25)]
        write_new_repos(tmp_path / "new-bigorg.yaml", "bigorg", repos)
        result = collect_summary(tmp_path)
        assert result["new_repos_count"] == 25
        assert "and 5 more" in result["new_repos"]

    def test_new_repo_missing_score_shows_na(self, tmp_path):
        write_new_repos(tmp_path / "new-myorg.yaml", "myorg", [("weird-repo", None)])
        result = collect_summary(tmp_path)
        assert "myorg/weird-repo (N/A)" in result["new_repos"]
```

Add a CLI-level test to `TestCollectSummaryCLI` (after `test_inaccessible_reported_in_output`):

```python
    def test_new_repos_reported_in_output(self, tmp_path):
        data = {"org": "testorg", "repos": [{"name": "fresh-repo", "score": 33.0}]}
        with open(tmp_path / "new-testorg.yaml", "w") as f:
            yaml.dump(data, f)
        output_file = tmp_path / "github_output"
        output_file.touch()
        env = {**os.environ, "GITHUB_OUTPUT": str(output_file)}
        result = subprocess.run(
            [sys.executable, self.SCRIPT, "--runner-dir", str(tmp_path)],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0
        content = output_file.read_text()
        assert "has_new_repos=true" in content
        assert "new_repos_count=1" in content
        assert "testorg/fresh-repo" in content
```

Add two tests to `TestEmitGhaAnnotations` (after `test_multiple_orgs`), and extend that class's imports are already satisfied since `write_new_repos` is imported at module level above:

```python
    def test_new_repo_emits_notice(self, tmp_path, capsys):
        write_new_repos(tmp_path / "new-myorg.yaml", "myorg", [("fresh-repo", 33.0)])
        emit_gha_annotations(tmp_path)
        out = capsys.readouterr().out
        assert "::notice title=new_repo::myorg/fresh-repo: scored 33.0" in out

    def test_new_repo_missing_score_annotation_shows_na(self, tmp_path, capsys):
        write_new_repos(tmp_path / "new-myorg.yaml", "myorg", [("weird-repo", None)])
        emit_gha_annotations(tmp_path)
        out = capsys.readouterr().out
        assert "::notice title=new_repo::myorg/weird-repo: scored N/A" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_collect_summary.py -v`
Expected: FAIL — `KeyError: 'has_new_repos'` and `AssertionError` on missing `::notice::` output

- [ ] **Step 3: Extend `collect_summary()` in `runner_lib.py`**

Replace `runner/runner_lib.py:448-480`:

```python
def collect_summary(runner_dir: Path) -> dict:
    """Read failed/inaccessible YAML files and return a structured summary."""
    failed_files = sorted(runner_dir.glob("failed-*.yaml"))
    inaccessible_files = sorted(runner_dir.glob("inaccessible-*.yaml"))

    failed_repos_list = []
    for f in failed_files:
        with open(f) as fh:
            data = yaml.safe_load(fh)
        org = data.get("org", "?")
        for repo in data.get("repos") or []:
            failed_repos_list.append(f"{org}/{repo}")

    inaccessible_count = 0
    for f in inaccessible_files:
        with open(f) as fh:
            data = yaml.safe_load(fh)
        inaccessible_count += len(data.get("repos") or [])

    failed_count = len(failed_repos_list)
    truncated = failed_repos_list[:20]
    repos_str = ", ".join(truncated)
    if failed_count > 20:
        repos_str += f" ... and {failed_count - 20} more"

    return {
        "has_failures": failed_count > 0,
        "failed_count": failed_count,
        "failed_repos": repos_str,
        "inaccessible_count": inaccessible_count,
        "failed_files": failed_files,
        "inaccessible_files": inaccessible_files,
    }
```

with:

```python
def collect_summary(runner_dir: Path) -> dict:
    """Read failed/inaccessible/new-repo YAML files and return a structured summary."""
    failed_files = sorted(runner_dir.glob("failed-*.yaml"))
    inaccessible_files = sorted(runner_dir.glob("inaccessible-*.yaml"))

    failed_repos_list = []
    for f in failed_files:
        with open(f) as fh:
            data = yaml.safe_load(fh)
        org = data.get("org", "?")
        for repo in data.get("repos") or []:
            failed_repos_list.append(f"{org}/{repo}")

    inaccessible_count = 0
    for f in inaccessible_files:
        with open(f) as fh:
            data = yaml.safe_load(fh)
        inaccessible_count += len(data.get("repos") or [])

    failed_count = len(failed_repos_list)
    truncated = failed_repos_list[:20]
    repos_str = ", ".join(truncated)
    if failed_count > 20:
        repos_str += f" ... and {failed_count - 20} more"

    new_entries = load_new_repos(runner_dir)
    new_repos_list = [
        f"{e['org']}/{e['name']} ({e['score'] if e['score'] is not None else 'N/A'})"
        for e in new_entries
    ]
    new_repos_count = len(new_repos_list)
    new_truncated = new_repos_list[:20]
    new_repos_str = ", ".join(new_truncated)
    if new_repos_count > 20:
        new_repos_str += f" ... and {new_repos_count - 20} more"

    return {
        "has_failures": failed_count > 0,
        "failed_count": failed_count,
        "failed_repos": repos_str,
        "inaccessible_count": inaccessible_count,
        "failed_files": failed_files,
        "inaccessible_files": inaccessible_files,
        "has_new_repos": new_repos_count > 0,
        "new_repos_count": new_repos_count,
        "new_repos": new_repos_str,
    }
```

(`load_new_repos` is defined later in the same module — fine, since Python resolves names inside a function body at call time, not definition time.)

- [ ] **Step 4: Extend `emit_gha_annotations()` in `collect_summary.py`**

Replace the import line at the top of `runner/collect_summary.py`:

```python
from runner_lib import validate_token, collect_summary, load_error_details
```

with:

```python
from runner_lib import validate_token, collect_summary, load_error_details, load_new_repos
```

Replace `emit_gha_annotations()`:

```python
def emit_gha_annotations(runner_dir: Path) -> None:
    """Read errors-*.json and emit ::error/::warning GHA annotations."""
    entries = load_error_details(runner_dir)
    for entry in entries:
        org = entry.get("org", "?")
        repo = entry.get("repo", "?")
        status = entry.get("status", "")
        category = entry.get("category", "")
        message = entry.get("message", "").replace("\n", " ").replace("\r", "").strip()

        if status == "failed":
            print(f"::error title={category}::{org}/{repo}: {message}")
        elif category in ("auth_forbidden", "auth_not_found"):
            print(f"::warning title={category}::{org}/{repo}: {message}")
        # empty and unchanged: no annotation
```

with:

```python
def emit_gha_annotations(runner_dir: Path) -> None:
    """Read errors-*.json/new-*.yaml and emit ::error/::warning/::notice GHA annotations."""
    entries = load_error_details(runner_dir)
    for entry in entries:
        org = entry.get("org", "?")
        repo = entry.get("repo", "?")
        status = entry.get("status", "")
        category = entry.get("category", "")
        message = entry.get("message", "").replace("\n", " ").replace("\r", "").strip()

        if status == "failed":
            print(f"::error title={category}::{org}/{repo}: {message}")
        elif category in ("auth_forbidden", "auth_not_found"):
            print(f"::warning title={category}::{org}/{repo}: {message}")
        # empty and unchanged: no annotation

    for entry in load_new_repos(runner_dir):
        score = entry["score"] if entry["score"] is not None else "N/A"
        print(f"::notice title=new_repo::{entry['org']}/{entry['name']}: scored {score}")
```

- [ ] **Step 5: Write the new `$GITHUB_OUTPUT` lines in `main()`**

In `runner/collect_summary.py`, replace the `output_lines` block:

```python
    output_lines = [
        f"inaccessible_count={summary['inaccessible_count']}",
        f"has_failures={'true' if summary['has_failures'] else 'false'}",
        f"failed_count={summary['failed_count']}",
        f"failed_repos={summary['failed_repos']}",
    ]
    write_github_output(output_lines)
```

with:

```python
    output_lines = [
        f"inaccessible_count={summary['inaccessible_count']}",
        f"has_failures={'true' if summary['has_failures'] else 'false'}",
        f"failed_count={summary['failed_count']}",
        f"failed_repos={summary['failed_repos']}",
        f"has_new_repos={'true' if summary['has_new_repos'] else 'false'}",
        f"new_repos_count={summary['new_repos_count']}",
        f"new_repos={summary['new_repos']}",
    ]
    write_github_output(output_lines)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_collect_summary.py -v`
Expected: all tests PASS

- [ ] **Step 7: Run the full test suite**

Run: `uv run pytest tests/ -v`
Expected: all tests PASS (e2e tests skip without `--run-e2e`)

- [ ] **Step 8: Commit**

```bash
git add runner/runner_lib.py runner/collect_summary.py tests/test_collect_summary.py
git commit -m "feat: aggregate new-repo status into GH outputs and annotations

collect_summary() adds has_new_repos/new_repos_count/new_repos.
emit_gha_annotations() emits ::notice:: per newly-scored repo.
collect_summary.py writes the three new fields to \$GITHUB_OUTPUT."
```

---

### Task 4: Add the Slack notification job to the scheduled workflow

**Files:**
- Modify: `.github/workflows/assess-scheduled.yml:48-51` (job outputs), `.github/workflows/assess-scheduled.yml:111-118` (commit step), append new job at end of file

**Interfaces:**
- Consumes: `has_new_repos`, `new_repos_count`, `new_repos` outputs from Task 3 (via `steps.summary.outputs.*`)
- Produces: `notify-new-repos` job in `assess-scheduled.yml`

- [ ] **Step 1: Add the three new outputs to the `assess` job**

Replace `.github/workflows/assess-scheduled.yml:48-51`:

```yaml
    outputs:
      failed_count: ${{ steps.summary.outputs.failed_count }}
      failed_repos: ${{ steps.summary.outputs.failed_repos }}
      has_failures: ${{ steps.summary.outputs.has_failures }}
```

with:

```yaml
    outputs:
      failed_count: ${{ steps.summary.outputs.failed_count }}
      failed_repos: ${{ steps.summary.outputs.failed_repos }}
      has_failures: ${{ steps.summary.outputs.has_failures }}
      new_repos_count: ${{ steps.summary.outputs.new_repos_count }}
      new_repos: ${{ steps.summary.outputs.new_repos }}
      has_new_repos: ${{ steps.summary.outputs.has_new_repos }}
```

- [ ] **Step 2: Commit `new-*.yaml` alongside `failed-*.yaml`/`inaccessible-*.yaml`**

Replace `.github/workflows/assess-scheduled.yml:111-118`:

```yaml
      - name: Commit failed/inaccessible repos YAML (if any)
        if: always()
        run: |
          files=$(ls runner/status/failed-*.yaml runner/status/inaccessible-*.yaml 2>/dev/null || true)
          if [ -n "$files" ]; then
            git add runner/status/failed-*.yaml runner/status/inaccessible-*.yaml 2>/dev/null || true
            git commit -m "chore: record failed/inaccessible repos from run ${{ github.run_id }}" || true
            git push || true
          fi
```

with:

```yaml
      - name: Commit failed/inaccessible/new repos YAML (if any)
        if: always()
        run: |
          files=$(ls runner/status/failed-*.yaml runner/status/inaccessible-*.yaml runner/status/new-*.yaml 2>/dev/null || true)
          if [ -n "$files" ]; then
            git add runner/status/failed-*.yaml runner/status/inaccessible-*.yaml runner/status/new-*.yaml 2>/dev/null || true
            git commit -m "chore: record failed/inaccessible/new repos from run ${{ github.run_id }}" || true
            git push || true
          fi
```

- [ ] **Step 3: Append the `notify-new-repos` job**

At the end of `.github/workflows/assess-scheduled.yml` (after the closing `}` of the existing `notify` job's payload, i.e. after the current last line), add:

```yaml

  notify-new-repos:
    runs-on: ubuntu-latest
    needs: assess
    if: success() && needs.assess.outputs.has_new_repos == 'true' && vars.SLACK_NOTIFICATIONS == 'true'
    steps:
      - name: Notify Slack of newly-scored repos
        uses: slackapi/slack-github-action@v2
        with:
          webhook: ${{ secrets.SLACK_WEBHOOK_URL }}
          webhook-type: incoming-webhook
          payload: |
            {
              "text": ":tada: *New repo(s) scored* — ${{ needs.assess.outputs.new_repos_count }} repo(s)",
              "attachments": [{
                "color": "good",
                "fields": [
                  { "title": "New repos", "value": "${{ needs.assess.outputs.new_repos }}", "short": false },
                  { "title": "Run summary", "value": "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}", "short": false }
                ]
              }]
            }
```

- [ ] **Step 4: Verify YAML syntax**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/assess-scheduled.yml')); print('Valid YAML')"`
Expected: `Valid YAML`

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/assess-scheduled.yml
git commit -m "feat: notify Slack when new repos get their first score

Scheduled workflow only. Gated on the existing SLACK_NOTIFICATIONS
variable — shares the toggle with failure notifications rather than
adding a second one. Also commits runner/status/new-*.yaml alongside
the existing failed-*.yaml/inaccessible-*.yaml."
```

---

### Task 5: Document the new-repo notification in the README

**Files:**
- Modify: `README.md:175-177`

- [ ] **Step 1: Expand the Notifications section**

Replace `README.md:175-177`:

```markdown
## Notifications

Slack failure notifications are sent when `SLACK_NOTIFICATIONS = 'true'` is set as a repository variable and `SLACK_WEBHOOK_URL` is configured as a secret. Notifications fire on both manual and scheduled workflow failures.
```

with:

```markdown
## Notifications

Slack failure notifications are sent when `SLACK_NOTIFICATIONS = 'true'` is set as a repository variable and `SLACK_WEBHOOK_URL` is configured as a secret. Notifications fire on both manual and scheduled workflow failures.

The scheduled workflow also sends a batched Slack message whenever any repository gets its first-ever successful score in that run, and emits a `::notice::` GitHub Actions annotation per newly-scored repo on both workflows. New-repo notifications share the same `SLACK_NOTIFICATIONS`/`SLACK_WEBHOOK_URL` configuration as failure notifications — there is no separate toggle, and no manual-workflow Slack message for new repos.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document the new-repo-scored Slack notification"
```

---

### Task 6: Extend the e2e test to cover `is_new`/`score`

**Files:**
- Modify: `tests/test_e2e.py`

- [ ] **Step 1: Extend `test_successful_assessment`**

Replace in `tests/test_e2e.py`:

```python
    def test_successful_assessment(self, tmp_path):
        """Assess a known small public repo end-to-end."""
        result = assess_repo("konflux-ci", "build-definitions", tmp_path)
        assert isinstance(result, AssessmentResult)
        assert result.status == "succeeded", f"Expected succeeded, got {result.status}: {result.message}"
        assert result.output_path != ""
        assert Path(result.output_path).exists()
        latest = tmp_path / "konflux-ci" / "build-definitions" / "assessment-latest.json"
        assert latest.is_symlink()
```

with:

```python
    def test_successful_assessment(self, tmp_path):
        """Assess a known small public repo end-to-end."""
        result = assess_repo("konflux-ci", "build-definitions", tmp_path)
        assert isinstance(result, AssessmentResult)
        assert result.status == "succeeded", f"Expected succeeded, got {result.status}: {result.message}"
        assert result.output_path != ""
        assert Path(result.output_path).exists()
        latest = tmp_path / "konflux-ci" / "build-definitions" / "assessment-latest.json"
        assert latest.is_symlink()
        # tmp_path is fresh for every test — no prior assessment-latest.json existed
        assert result.is_new is True
        assert isinstance(result.score, (int, float))
```

- [ ] **Step 2: Run the e2e test to verify it passes**

Requires `GH_TOKEN` in `.env` (see `env.example`) and `podman` installed.

Run: `uv run pytest tests/test_e2e.py::TestE2EPipeline::test_successful_assessment --run-e2e -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_e2e.py
git commit -m "test: assert is_new/score in the e2e successful-assessment test"
```

---

### Task 7: Full verification

- [ ] **Step 1: Run the complete unit test suite**

Run: `uv run pytest tests/ -v`
Expected: all tests PASS (e2e tests skipped without `--run-e2e`)

- [ ] **Step 2: Run the e2e suite (requires GH_TOKEN + podman)**

Run: `uv run pytest tests/ --run-e2e -v`
Expected: all tests PASS

- [ ] **Step 3: Re-verify workflow YAML syntax for both workflows**

Run: `python3 -c "import yaml; [yaml.safe_load(open(f)) for f in ['.github/workflows/assess-scheduled.yml', '.github/workflows/assess-manual.yml']]; print('Valid YAML')"`
Expected: `Valid YAML`

- [ ] **Step 4: Confirm `assess-manual.yml` is untouched**

Run: `git diff main -- .github/workflows/assess-manual.yml`
Expected: no output (empty diff)
