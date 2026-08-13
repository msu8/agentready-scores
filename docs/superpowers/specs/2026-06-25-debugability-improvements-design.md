# Debugability Improvements for Assessment Pipeline — Design Spec

**Date:** 2026-06-25
**Status:** Approved

---

## Overview

The assessment pipeline currently loses critical error information at every stage: `git clone` stderr is captured but never logged, `failed-*.yaml` files record only repo names without error reasons, and all failure modes (auth, timeout, container crash, missing output) are lumped together as generic "failed". Operators must re-run the pipeline to diagnose what went wrong.

This spec adds structured error capture, coarse failure categorization, and GitHub Actions annotations so that operators can diagnose failures directly from the CI run summary — without re-running anything and without cluttering the existing YAML files.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      assess.py                              │
│                                                             │
│  process_org()                                              │
│    ├─ run_batch() → list[AssessmentResult]                  │
│    ├─ write_failed_repos()        (unchanged — lean YAML)   │
│    └─ write_error_details()       (NEW — errors-<org>.json) │
└─────────────────────────────────────────────────────────────┘
                           │
                    errors-<org>.json (internal, not committed)
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                   collect_summary.py                        │
│                                                             │
│  main()                                                     │
│    ├─ collect_summary()           (existing)                │
│    └─ emit_gha_annotations()      (NEW — reads errors JSON) │
│         └─ prints ::error / ::warning lines to stdout       │
└─────────────────────────────────────────────────────────────┘
```

**Data flow:** `assess_repo()` catches exceptions and returns `AssessmentResult` objects. `run_batch()` collects these and returns them. `assess.py` writes the error details to `errors-<org>.json` (internal, never committed or uploaded). `collect_summary.py` reads all `errors-*.json` files and emits GHA annotations for each failure.

**What stays unchanged:** `failed-*.yaml` and `inaccessible-*.yaml` remain lean (org + repo list only). No new fields, no error details in YAML. The workflow YAML files need no changes.

---

## Error Model

### AssessmentResult dataclass

New dataclass in `runner_lib.py`:

```python
@dataclasses.dataclass
class AssessmentResult:
    repo: str
    status: str          # "succeeded" | "failed" | "skipped"
    category: str        # reason code (see below)
    message: str         # stderr snippet or human-readable explanation, max 500 chars
    output_path: str     # path to assessment JSON (succeeded) or empty string
```

**`status` values (3):**
- `"succeeded"` — assessment completed, JSON written
- `"failed"` — assessment attempted but errored
- `"skipped"` — assessment not attempted (auth issue, empty repo, unchanged)

**`category` values (7):**

| Category | Status | When |
|---|---|---|
| `clone_failure` | failed | `git clone` raises `CalledProcessError` or `TimeoutExpired` |
| `container_failure` | failed | `podman run` raises `CalledProcessError` or `TimeoutExpired` |
| `output_missing` | failed | Container succeeds but no `assessment-*.json` found |
| `auth_forbidden` | skipped | `check_repo_access()` returns HTTP 403 |
| `auth_not_found` | skipped | `check_repo_access()` returns HTTP 404 or network error |
| `empty` | skipped | Repo has no commits (HEAD parse fails) |
| `unchanged` | skipped | HEAD matches prior `assessment-latest.json` commit hash |

**`message` field:** Contains the first 500 characters of stderr for `clone_failure` and `container_failure`, or a brief human-readable explanation for other categories (e.g. "repo has no commits").

---

## Error Capture

### `check_repo_access()` — 3-way return

Currently returns `bool`. Changes to return a string:

```python
def check_repo_access(org: str, repo: str) -> str:
    """Return 'accessible', 'forbidden', or 'not_found'."""
```

- HTTP 200 → `"accessible"`
- HTTP 403 → `"forbidden"`
- HTTP 404 or any `RequestException` → `"not_found"`

### `assess_repo()` — returns `AssessmentResult`

Currently returns `str` or raises exceptions. Changes to always return `AssessmentResult`:

```python
def assess_repo(org: str, repo: str, output_dir: Path) -> AssessmentResult:
```

- Wraps the clone step in `try/except (CalledProcessError, subprocess.TimeoutExpired)` → extracts `e.stderr`, returns `AssessmentResult(status="failed", category="clone_failure", message=stderr[:500])`
- Same pattern for the podman step → `category="container_failure"`
- `FileNotFoundError` for missing output → `category="output_missing"`
- Access check: maps `"forbidden"` → `category="auth_forbidden"`, `"not_found"` → `category="auth_not_found"`
- Empty/unchanged repos → `category="empty"` / `category="unchanged"`
- Success → `AssessmentResult(status="succeeded", category="", message="", output_path=str(dest_json))`

### `run_batch()` — receives `AssessmentResult` objects

Currently returns `Tuple[List[str], List[str], List[str]]` (succeeded, failed, inaccessible).

Changes to also return the full list of `AssessmentResult` objects:

```python
def run_batch(...) -> Tuple[List[str], List[str], List[str], List[AssessmentResult]]:
    # Returns (succeeded, failed, inaccessible, results)
```

The first three lists preserve backward compatibility with `assess.py` and `collect_summary.py`. The fourth element is the raw results list for `write_error_details()`.

---

## Internal Error Details File

### `write_error_details()`

New function in `runner_lib.py`:

```python
def write_error_details(path: Path, org: str, results: list[AssessmentResult]) -> None:
```

Writes `errors-<org>.json` with structure:

```json
{
  "org": "konflux-ci",
  "timestamp": "2026-06-25T10:30:00Z",
  "errors": [
    {
      "repo": "broken-repo",
      "status": "failed",
      "category": "clone_failure",
      "message": "fatal: repository 'https://...' not found"
    }
  ]
}
```

Only includes results where `status != "succeeded"`. File is written to `runner/errors-<org>.json` alongside `failed-*.yaml`. It is NOT committed or uploaded — it's a transient inter-process communication file read by `collect_summary.py` in the same CI job.

### `load_error_details()`

New function in `runner_lib.py`:

```python
def load_error_details(runner_dir: Path) -> list[dict]:
```

Reads all `errors-*.json` files in the directory and returns a flat list of error entries (each entry includes the `org` field from its parent file).

---

## Annotation Layer

### `emit_gha_annotations()`

New function in `collect_summary.py`:

```python
def emit_gha_annotations(runner_dir: Path) -> None:
```

Reads error details via `load_error_details()` and emits GHA annotation lines to stdout:

- **Failures** (`status == "failed"`):
  ```
  ::error title=clone_failure::konflux-ci/broken-repo: fatal: repository not found
  ```

- **Auth issues** (`status == "skipped"`, category `auth_forbidden` or `auth_not_found`):
  ```
  ::warning title=auth_forbidden::konflux-ci/private-repo: HTTP 403 — token lacks access
  ```

- **Other skips** (`empty`, `unchanged`): No annotation — these are normal operational states.

Annotations appear in the GHA run summary sidebar, grouped by title. Operators can click to see the full message.

### Integration in `collect_summary.py`

`emit_gha_annotations()` is called in `main()` after `collect_summary()`, unconditionally (it's a no-op when no error files exist).

---

## Testing

### Unit Tests

All new functions get unit tests in `tests/test_runner_lib.py` and `tests/test_collect_summary.py`:

| Function | Key test cases |
|---|---|
| `check_repo_access()` | HTTP 200 → accessible, 403 → forbidden, 404 → not_found, network error → not_found |
| `assess_repo()` | Clone failure → AssessmentResult with stderr, container failure, output missing, auth forbidden/not_found, empty, unchanged, success |
| `run_batch()` | Mixed results (some succeed, some fail, some skipped), retry logic only retries failures |
| `write_error_details()` | Correct JSON structure, only non-succeeded entries, empty results → no file |
| `load_error_details()` | Reads multiple files, returns flat list with org field |
| `emit_gha_annotations()` | Correct `::error`/`::warning` format, skips empty/unchanged, no-op when no files |

Tests for `assess_repo()` mock `subprocess.run` and `check_repo_access()` to avoid real git/podman calls.

### E2E Tests

Full pipeline tests that clone a real repo, run the podman container, and verify the output:

- **Location:** `tests/test_e2e.py`
- **Marker:** `@pytest.mark.e2e` — skipped unless `--run-e2e` is passed
- **Secrets:** Loaded via direnv from `.envrc` (gitignored). An `env.example` documents required variables (`GH_TOKEN`).
- **Scope:** A small set of known repos (2-3) covering success and currently failing repo.
- **Runtime dependency:** Requires `podman` available on the system.

**conftest.py setup:**

```python
def pytest_addoption(parser):
    parser.addoption("--run-e2e", action="store_true", default=False)

def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-e2e"):
        skip = pytest.mark.skip(reason="needs --run-e2e")
        for item in items:
            if "e2e" in item.keywords:
                item.add_marker(skip)
```

---

## Files Changed

| File | Change |
|---|---|
| `runner/runner_lib.py` | Add `AssessmentResult` dataclass. Modify `check_repo_access()` (3-way return). Modify `assess_repo()` (return `AssessmentResult`). Modify `run_batch()` (add results to return tuple). Add `write_error_details()`, `load_error_details()`. |
| `runner/assess.py` | Call `write_error_details()` in `process_org()` after `write_failed_repos()`. |
| `runner/collect_summary.py` | Add `emit_gha_annotations()`. Call it in `main()`. |
| `tests/test_runner_lib.py` | Add tests for modified and new functions. |
| `tests/test_collect_summary.py` | Add tests for `emit_gha_annotations()`. |
| `tests/test_e2e.py` | New — full pipeline e2e tests. |
| `tests/conftest.py` | New or modified — add `--run-e2e` option and e2e skip logic. |
| `env.example` | New — documents required env vars for e2e tests. |

**No changes to:**
- `.github/workflows/assess-scheduled.yml`
- `.github/workflows/assess-manual.yml`
- `failed-*.yaml` / `inaccessible-*.yaml` format

---

## Estimated Size

~120 lines of production code changes + ~150 lines of new unit tests + ~50 lines of e2e test scaffolding. The change is additive — no existing behavior is removed, only enriched.
