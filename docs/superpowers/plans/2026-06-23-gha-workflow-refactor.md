# GHA Workflow Refactor — Extract Inline Python into Testable Scripts

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace inline Python heredocs and shell logic in GHA workflows with proper Python functions (in `runner_lib.py`) called via a thin CLI script, making CI logic testable with pytest.

**Architecture:** Add two library functions to `runner_lib.py` — `validate_token()` (checks GH_TOKEN against `/user` API) and `collect_summary()` (reads `runner/failed-*.yaml` and `runner/inaccessible-*.yaml`, returns structured data). A new CLI script `runner/collect_summary.py` calls these functions, prints human-readable output, writes `GITHUB_OUTPUT` lines, and sets the exit code. Both GHA workflows simplify to one-liner `python3 runner/collect_summary.py [--validate-token]` calls.

**Tech Stack:** Python 3.11, pytest, PyYAML, requests (all already in `runner/requirements.txt`)

## Global Constraints

- Python 3.11+ (workflow uses `setup-python` with `3.11`)
- Dependencies limited to what's already in `runner/requirements.txt`
- Tests run with `uv run pytest` locally
- All new files go under `runner/` or `tests/` — nothing in repo root

---

### Task 1: Add `validate_token()` to `runner_lib.py` with tests

**Files:**
- Modify: `runner/runner_lib.py` (append function at end)
- Create: `tests/test_collect_summary.py`

**Interfaces:**
- Consumes: `GH_TOKEN` environment variable
- Produces: `validate_token() -> bool` — returns `True` if token is valid, `False` otherwise. Prints error details to stderr.

- [ ] **Step 1: Write failing tests for `validate_token`**

Create `tests/test_collect_summary.py`:

```python
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from runner_lib import validate_token


class TestValidateToken:
    def test_valid_token(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("runner_lib.requests.get", return_value=mock_resp) as mock_get, \
             patch.dict("os.environ", {"GH_TOKEN": "ghp_valid123"}):
            assert validate_token() is True
            mock_get.assert_called_once()
            url = mock_get.call_args[0][0]
            assert url == "https://api.github.com/user"

    def test_expired_token(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        with patch("runner_lib.requests.get", return_value=mock_resp), \
             patch.dict("os.environ", {"GH_TOKEN": "ghp_expired"}):
            assert validate_token() is False

    def test_missing_token(self):
        with patch.dict("os.environ", {}, clear=True):
            assert validate_token() is False

    def test_network_error(self):
        import requests as req
        with patch("runner_lib.requests.get", side_effect=req.ConnectionError), \
             patch.dict("os.environ", {"GH_TOKEN": "ghp_valid"}):
            assert validate_token() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_collect_summary.py::TestValidateToken -v`
Expected: FAIL with `ImportError: cannot import name 'validate_token'`

- [ ] **Step 3: Implement `validate_token()` in `runner_lib.py`**

Append to the end of `runner/runner_lib.py`:

```python
def validate_token() -> bool:
    """Check if GH_TOKEN is set and valid by calling the /user endpoint."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: GH_TOKEN is not set.", file=sys.stderr)
        return False
    try:
        resp = requests.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"ERROR: GH_TOKEN is invalid or expired (HTTP {resp.status_code}).", file=sys.stderr)
            return False
        return True
    except requests.RequestException as e:
        print(f"ERROR: Failed to validate GH_TOKEN: {e}", file=sys.stderr)
        return False
```

Also add `import sys` to the imports at the top of `runner_lib.py` (it's not currently imported).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_collect_summary.py::TestValidateToken -v`
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
git add runner/runner_lib.py tests/test_collect_summary.py
git commit -m "feat(runner): add validate_token() with tests"
```

---

### Task 2: Add `collect_summary()` to `runner_lib.py` with tests

**Files:**
- Modify: `runner/runner_lib.py` (append function at end)
- Modify: `tests/test_collect_summary.py` (add test class)

**Interfaces:**
- Consumes: `runner_dir` — `Path` to the directory containing `failed-*.yaml` and `inaccessible-*.yaml`
- Produces: `collect_summary(runner_dir: Path) -> dict` with keys:
  - `has_failures: bool`
  - `failed_count: int`
  - `failed_repos: str` (comma-separated `org/repo`, max 20, with "and N more" suffix)
  - `inaccessible_count: int`
  - `failed_files: list[Path]`
  - `inaccessible_files: list[Path]`

- [ ] **Step 1: Write failing tests for `collect_summary`**

Append to `tests/test_collect_summary.py`:

```python
import yaml
from runner_lib import collect_summary


class TestCollectSummary:
    def _write_yaml(self, path, org, repos):
        data = {"org": org, "repos": repos}
        with open(path, "w") as f:
            yaml.dump(data, f)

    def test_no_files(self, tmp_path):
        result = collect_summary(tmp_path)
        assert result["has_failures"] is False
        assert result["failed_count"] == 0
        assert result["failed_repos"] == ""
        assert result["inaccessible_count"] == 0

    def test_failures_only(self, tmp_path):
        self._write_yaml(tmp_path / "failed-myorg.yaml", "myorg", ["repo-a", "repo-b"])
        result = collect_summary(tmp_path)
        assert result["has_failures"] is True
        assert result["failed_count"] == 2
        assert "myorg/repo-a" in result["failed_repos"]
        assert "myorg/repo-b" in result["failed_repos"]
        assert result["inaccessible_count"] == 0

    def test_inaccessible_only(self, tmp_path):
        self._write_yaml(tmp_path / "inaccessible-myorg.yaml", "myorg", ["private-1"])
        result = collect_summary(tmp_path)
        assert result["has_failures"] is False
        assert result["failed_count"] == 0
        assert result["inaccessible_count"] == 1

    def test_both_types(self, tmp_path):
        self._write_yaml(tmp_path / "failed-orgA.yaml", "orgA", ["r1"])
        self._write_yaml(tmp_path / "inaccessible-orgA.yaml", "orgA", ["r2", "r3"])
        result = collect_summary(tmp_path)
        assert result["has_failures"] is True
        assert result["failed_count"] == 1
        assert result["inaccessible_count"] == 2

    def test_multiple_orgs(self, tmp_path):
        self._write_yaml(tmp_path / "failed-orgA.yaml", "orgA", ["r1"])
        self._write_yaml(tmp_path / "failed-orgB.yaml", "orgB", ["r2", "r3"])
        result = collect_summary(tmp_path)
        assert result["failed_count"] == 3

    def test_failed_repos_truncated_at_20(self, tmp_path):
        repos = [f"repo-{i}" for i in range(25)]
        self._write_yaml(tmp_path / "failed-bigorg.yaml", "bigorg", repos)
        result = collect_summary(tmp_path)
        assert result["failed_count"] == 25
        assert "and 5 more" in result["failed_repos"]

    def test_empty_repos_list(self, tmp_path):
        self._write_yaml(tmp_path / "failed-empty.yaml", "empty", [])
        result = collect_summary(tmp_path)
        assert result["has_failures"] is False
        assert result["failed_count"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_collect_summary.py::TestCollectSummary -v`
Expected: FAIL with `ImportError: cannot import name 'collect_summary'`

- [ ] **Step 3: Implement `collect_summary()` in `runner_lib.py`**

Append to the end of `runner/runner_lib.py`:

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

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_collect_summary.py::TestCollectSummary -v`
Expected: 7 PASSED

- [ ] **Step 5: Run all tests to check for regressions**

Run: `uv run pytest tests/ -v`
Expected: All tests pass (existing `test_runner_lib.py` + new `test_collect_summary.py`)

- [ ] **Step 6: Commit**

```bash
git add runner/runner_lib.py tests/test_collect_summary.py
git commit -m "feat(runner): add collect_summary() with tests"
```

---

### Task 3: Create `runner/collect_summary.py` CLI script with tests

**Files:**
- Create: `runner/collect_summary.py`
- Modify: `tests/test_collect_summary.py` (add CLI tests)

**Interfaces:**
- Consumes: `validate_token()` and `collect_summary()` from `runner_lib.py`
- Produces: CLI that:
  - `python3 runner/collect_summary.py --validate-token` — validates GH_TOKEN, exits 0/1
  - `python3 runner/collect_summary.py` — collects summary, writes `GITHUB_OUTPUT`, exits 0 (no failures) or 1 (has failures)
  - `python3 runner/collect_summary.py --runner-dir <path>` — override runner dir (default: `runner/` relative to script)

- [ ] **Step 1: Write failing tests for the CLI**

Append to `tests/test_collect_summary.py`:

```python
import subprocess
import os


class TestCollectSummaryCLI:
    SCRIPT = str(Path(__file__).resolve().parent.parent / "runner" / "collect_summary.py")

    def test_no_failures_exit_0(self, tmp_path):
        output_file = tmp_path / "github_output"
        output_file.touch()
        env = {**os.environ, "GITHUB_OUTPUT": str(output_file)}
        result = subprocess.run(
            [sys.executable, self.SCRIPT, "--runner-dir", str(tmp_path)],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0
        content = output_file.read_text()
        assert "has_failures=false" in content
        assert "failed_count=0" in content

    def test_failures_exit_1(self, tmp_path):
        data = {"org": "testorg", "repos": ["broken-repo"]}
        with open(tmp_path / "failed-testorg.yaml", "w") as f:
            yaml.dump(data, f)
        output_file = tmp_path / "github_output"
        output_file.touch()
        env = {**os.environ, "GITHUB_OUTPUT": str(output_file)}
        result = subprocess.run(
            [sys.executable, self.SCRIPT, "--runner-dir", str(tmp_path)],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 1
        content = output_file.read_text()
        assert "has_failures=true" in content
        assert "failed_count=1" in content
        assert "testorg/broken-repo" in content

    def test_inaccessible_reported_in_output(self, tmp_path):
        data = {"org": "testorg", "repos": ["private-1", "private-2"]}
        with open(tmp_path / "inaccessible-testorg.yaml", "w") as f:
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
        assert "inaccessible_count=2" in content

    def test_no_github_output_still_works(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_OUTPUT"}
        result = subprocess.run(
            [sys.executable, self.SCRIPT, "--runner-dir", str(tmp_path)],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_collect_summary.py::TestCollectSummaryCLI -v`
Expected: FAIL (script doesn't exist yet)

- [ ] **Step 3: Create `runner/collect_summary.py`**

```python
#!/usr/bin/env python3
"""Post-assessment CI utilities: token validation and result summary."""
import argparse
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from runner_lib import validate_token, collect_summary


def write_github_output(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as f:
        for line in lines:
            f.write(line + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--validate-token", action="store_true",
        help="Validate GH_TOKEN and exit",
    )
    parser.add_argument(
        "--runner-dir", type=Path, default=SCRIPT_DIR,
        help="Directory containing failed-*.yaml and inaccessible-*.yaml",
    )
    args = parser.parse_args()

    if args.validate_token:
        if validate_token():
            print("GH_TOKEN is valid.")
            return 0
        return 1

    summary = collect_summary(args.runner_dir)

    if summary["inaccessible_count"] > 0:
        print(f"Inaccessible repos (token lacks access): {summary['inaccessible_count']}")
        for f in summary["inaccessible_files"]:
            print(f"  {f}")

    output_lines = [
        f"inaccessible_count={summary['inaccessible_count']}",
        f"has_failures={'true' if summary['has_failures'] else 'false'}",
        f"failed_count={summary['failed_count']}",
        f"failed_repos={summary['failed_repos']}",
    ]
    write_github_output(output_lines)

    if summary["has_failures"]:
        print(f"Some repos failed assessment ({summary['failed_count']}):")
        for f in summary["failed_files"]:
            print(f"  {f}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_collect_summary.py::TestCollectSummaryCLI -v`
Expected: 4 PASSED

- [ ] **Step 5: Run all tests**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add runner/collect_summary.py tests/test_collect_summary.py
git commit -m "feat(runner): add collect_summary.py CLI for CI steps"
```

---

### Task 4: Simplify both GHA workflows to use the new scripts

**Files:**
- Modify: `.github/workflows/assess-scheduled.yml`
- Modify: `.github/workflows/assess-manual.yml`

**Interfaces:**
- Consumes: `runner/collect_summary.py` (from Task 3)
- Produces: Simplified workflow YAML with no inline Python heredocs

- [ ] **Step 1: Update `assess-scheduled.yml`**

Replace the "Validate GH_TOKEN" step (lines 85–101) with:

```yaml
      - name: Validate GH_TOKEN
        env:
          GH_TOKEN: ${{ secrets.GH_TOKEN }}
        run: python3 runner/collect_summary.py --validate-token
```

Replace the "Collect result summary" step (lines 119–164) with:

```yaml
      - name: Collect result summary
        if: always()
        id: summary
        run: python3 runner/collect_summary.py
```

- [ ] **Step 2: Update `assess-manual.yml`**

Replace the "Validate GH_TOKEN" step (lines 73–89) with:

```yaml
      - name: Validate GH_TOKEN
        env:
          GH_TOKEN: ${{ secrets.GH_TOKEN }}
        run: python3 runner/collect_summary.py --validate-token
```

Replace the "Collect result summary" step (lines 107–152) with:

```yaml
      - name: Collect result summary
        if: always()
        id: summary
        run: python3 runner/collect_summary.py
```

- [ ] **Step 3: Verify YAML is valid**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/assess-scheduled.yml')); yaml.safe_load(open('.github/workflows/assess-manual.yml')); print('OK')"` 

Expected: `OK`

- [ ] **Step 4: Run all tests one final time**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/assess-scheduled.yml .github/workflows/assess-manual.yml
git commit -m "refactor(ci): replace inline Python heredocs with collect_summary.py"
```

---

### Summary of changes

| Before | After |
|---|---|
| ~40 lines of inline Python heredocs per workflow | `python3 runner/collect_summary.py` one-liner |
| Shell + curl token validation (10 lines) | `python3 runner/collect_summary.py --validate-token` |
| Untestable CI logic | 15+ pytest tests covering validation, summary, CLI |
| Duplicated logic in two workflow files | Single script, called from both workflows |
