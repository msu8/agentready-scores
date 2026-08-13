import copy
import dataclasses
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import requests
import yaml

RUNNER_DIR = Path(__file__).parent

CONFIG_SEARCH_PATHS = [
    ".agentready/config/.agentready-config.yaml",
    ".agentready-config.yaml",
]


class SchemaError(ValueError):
    """Raised when a repos YAML file fails schema validation."""


@dataclasses.dataclass
class AssessmentResult:
    repo: str
    status: str
    category: str
    message: str
    output_path: str
    is_new: bool = False
    score: Optional[float] = None


def _validate_repos_yaml(data: dict, path: Path) -> None:
    """
    Validate the structure of a repos YAML file.

    Required keys:
      org   — non-empty string

    Optional keys:
      repos          — list of strings (repo names)
      exclude        — list of strings (repo names to skip)
      default_config — string path (relative to runner/) to a fallback
                        agentready config, applied to repos with no config of
                        their own

    Raises SchemaError with a descriptive message on any violation.
    """
    errors = []

    if not isinstance(data, dict):
        raise SchemaError(f"{path}: expected a YAML mapping at the top level, got {type(data).__name__}")

    # org
    org = data.get("org")
    if not org:
        errors.append("'org' is required and must be a non-empty string")
    elif not isinstance(org, str):
        errors.append(f"'org' must be a string, got {type(org).__name__}")

    # repos (optional)
    repos = data.get("repos")
    if repos is not None:
        if not isinstance(repos, list):
            errors.append(f"'repos' must be a list, got {type(repos).__name__}")
        else:
            bad = [r for r in repos if not isinstance(r, str)]
            if bad:
                errors.append(f"'repos' entries must be strings, got: {bad}")

    # exclude (optional)
    exclude = data.get("exclude")
    if exclude is not None:
        if not isinstance(exclude, list):
            errors.append(f"'exclude' must be a list, got {type(exclude).__name__}")
        else:
            bad = [r for r in exclude if not isinstance(r, str)]
            if bad:
                errors.append(f"'exclude' entries must be strings, got: {bad}")

    # default_config (optional)
    default_config = data.get("default_config")
    if default_config is not None:
        if not isinstance(default_config, str):
            errors.append(f"'default_config' must be a string, got {type(default_config).__name__}")
        elif not (RUNNER_DIR / default_config).exists():
            errors.append(f"'default_config' file not found: {RUNNER_DIR / default_config}")

    # unknown keys
    known = {"org", "repos", "exclude", "default_config"}
    unknown = set(data.keys()) - known
    if unknown:
        errors.append(f"unknown key(s): {sorted(unknown)}")

    if errors:
        msg = f"{path}: schema validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        raise SchemaError(msg)


def _find_repo_config(clone_dir: Path) -> Optional[Path]:
    """Return the absolute config path if found in the cloned repo, else None."""
    for rel_path in CONFIG_SEARCH_PATHS:
        candidate = clone_dir / rel_path
        if candidate.exists():
            return candidate
    return None


def _load_yaml_config(path: Path) -> Optional[dict]:
    """Load a YAML config file. Returns None (with a warning) if missing/unparseable."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        if data is None:
            return {}
        if not isinstance(data, dict):
            print(f"WARNING: {path} did not parse to a mapping — ignoring")
            return None
        return data
    except (OSError, yaml.YAMLError) as e:
        print(f"WARNING: {path} could not be read/parsed ({e}) — ignoring")
        return None


def load_repos_from_yaml(path: Path) -> Tuple[str, List[str], set, Optional[dict]]:
    """
    Load org, repo list, exclusions, and fallback config from a YAML file.

    Expected structure:
        org: my-org
        repos:          # optional — if absent, caller should use org discovery
          - repo-a
          - repo-b
        exclude:        # optional — repos to skip in any mode
          - archived-repo
        default_config: configs/my-org-default-config.yaml   # optional, relative to runner/
        # NOTE: keep default_config files out of runner/orgs/ — the scheduled
        # workflow globs runner/orgs/*.yaml as org files and will fail schema
        # validation on anything there that isn't one.

    Returns (org, repos, exclusions, default_config).
    If 'repos' is absent, returns an empty list — caller decides whether to
    discover repos from the org and apply the returned exclusions.
    default_config is the parsed contents of the referenced fallback config
    file (e.g. containing an `adr_source` block), or None if not set /
    unparseable. It only applies to repos that have no config of their own —
    see `assess_repo`.

    Raises SchemaError if the file structure is invalid.
    """
    with open(path) as f:
        data = yaml.safe_load(f)

    _validate_repos_yaml(data, path)

    org = data["org"]
    repos = data.get("repos") or []
    exclude = set(data.get("exclude") or [])

    if exclude and repos:
        repos = [r for r in repos if r not in exclude]

    default_config = None
    default_config_rel = data.get("default_config")
    if default_config_rel:
        default_config = _load_yaml_config(RUNNER_DIR / default_config_rel)

    return org, repos, exclude, default_config


def load_exclusions(path: Path) -> set:
    """Return the exclude set from a YAML file, empty set if key absent."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return set(data.get("exclude", []))


# Alias used by assess.py --from-file
load_repos_from_file = load_repos_from_yaml

# Backwards-compatible alias
load_demo_repos = load_repos_from_yaml


def discover_org_repos(org: str) -> List[str]:
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


# Backwards-compatible alias
discover_prod_repos = discover_org_repos


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


def _prior_commit_hash(latest_json: Path) -> Optional[str]:
    """
    Return repository.commit_hash from an existing assessment-latest.json,
    or None if the file doesn't exist or can't be parsed.
    """
    try:
        with open(latest_json.resolve()) as f:
            return json.load(f).get("repository", {}).get("commit_hash")
    except Exception:
        return None


def _read_overall_score(json_path: Path) -> Optional[float]:
    """Return overall_score from an assessment JSON file, or None if unreadable."""
    try:
        with open(json_path) as f:
            return json.load(f).get("overall_score")
    except Exception:
        return None


def assess_repo(
    org: str,
    repo: str,
    output_dir: Path,
    default_config: Optional[dict] = None,
    adr_clone_dir_default: Optional[Path] = None,
) -> AssessmentResult:
    """
    Clone repo, run agentready container, extract JSON, write to submissions dir.
    Always returns an AssessmentResult — never raises.

    Config resolution (repo config wins entirely — no merging with fallback):
      1. Look for `.agentready/config/.agentready-config.yaml` or
         `.agentready-config.yaml` in the cloned repo.
      2. If found, use it as-is (mounted into the container via --config).
      3. If not found and `default_config` has an `adr_source`, synthesize a
         minimal config from it instead.
      4. If the resolved config has an `adr_source`, its ADR repo is cloned
         (reusing `adr_clone_dir_default` when it matches the fallback's repo,
         otherwise cloned fresh for this repo) and mounted at /adr-repo, with
         `adr_source.repo` patched to that container path.
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

        # Shallow clone — use a credential helper to authenticate without
        # exposing the token in the URL or process arguments
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        clone_url = f"https://github.com/{org}/{repo}.git"
        clone_extra = []
        clone_env = None
        if token:
            helper = Path(tmp) / "credential-helper.sh"
            helper.write_text(
                "#!/bin/sh\n"
                "echo username=x-access-token\n"
                'echo "password=$GIT_CLONE_TOKEN"\n'
            )
            helper.chmod(0o755)
            clone_env = {**os.environ, "GIT_CLONE_TOKEN": token}
            clone_extra = ["-c", "credential.helper=", "-c", f"credential.helper=!'{helper}'"]
        try:
            subprocess.run(
                ["git"] + clone_extra + ["clone", "--depth=1", clone_url, str(clone_dir)],
                check=True,
                capture_output=True,
                timeout=120,
                env=clone_env,
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
        # Computed via existence, not via prior_hash being None — a corrupted or
        # unparseable existing file must not be misreported as "new".
        is_new = not existing_latest.exists()
        prior_hash = _prior_commit_hash(existing_latest)
        if prior_hash and head_hash == prior_hash:
            return AssessmentResult(repo=repo, status="skipped", category="unchanged",
                                    message=f"HEAD {head_hash[:8]} matches prior assessment",
                                    output_path="")

        uid = subprocess.check_output(["id", "-u"]).decode().strip()
        gid = subprocess.check_output(["id", "-g"]).decode().strip()

        # --- Config discovery + adr_source resolution (config.md Sections 1-4) ---
        config_path_in_container: Optional[str] = None
        extra_mounts: List[str] = []
        tmp_adr_clone: Optional[tempfile.TemporaryDirectory] = None

        try:
            repo_config_path = _find_repo_config(clone_dir)
            repo_config = _load_yaml_config(repo_config_path) if repo_config_path else None
        except Exception as e:
            print(f"WARNING: failed to read config for {repo}: {e}")
            repo_config_path, repo_config = None, None

        # Own config wins entirely — fallback only applies when no repo config exists.
        resolved_config = repo_config
        if resolved_config is None and default_config and default_config.get("adr_source"):
            resolved_config = {"adr_source": default_config["adr_source"]}

        if resolved_config and resolved_config.get("adr_source"):
            adr_source = resolved_config["adr_source"]
            using_default_repo = bool(
                default_config
                and default_config.get("adr_source", {}).get("repo") == adr_source.get("repo")
            )

            adr_mount_dir: Optional[Path] = None
            if using_default_repo and adr_clone_dir_default:
                adr_mount_dir = adr_clone_dir_default
            else:
                try:
                    tmp_adr_clone = tempfile.TemporaryDirectory(prefix=f"agentready-adr-{repo}-")
                    adr_mount_dir = Path(tmp_adr_clone.name)
                    subprocess.run(
                        ["git", "clone", "--depth=1",
                         f"https://github.com/{adr_source['repo']}.git", str(adr_mount_dir)],
                        check=True, capture_output=True, timeout=120,
                    )
                except Exception as e:
                    print(f"WARNING: ADR repo clone failed for {repo} ({adr_source.get('repo')}): {e}")
                    adr_mount_dir = None

            if adr_mount_dir:
                try:
                    patched = copy.deepcopy(resolved_config)
                    patched["adr_source"]["repo"] = "/adr-repo"
                    patched_config_path = Path(tmp) / "patched-config.yaml"
                    with open(patched_config_path, "w") as f:
                        yaml.dump(patched, f)
                    extra_mounts += [
                        "-v", f"{adr_mount_dir}:/adr-repo:ro,z",
                        "-v", f"{patched_config_path}:/agentready-config.yaml:ro,z",
                    ]
                    config_path_in_container = "/agentready-config.yaml"
                except Exception as e:
                    print(f"WARNING: failed to write patched config for {repo}: {e}")
            # else: ADR clone failed — fall through, no --config passed for this repo.

        elif resolved_config:
            extra_mounts += ["-v", f"{repo_config_path}:/agentready-config.yaml:ro,z"]
            config_path_in_container = "/agentready-config.yaml"

        # Run agentready container — pipe "y" to auto-confirm large-repo prompt
        cmd = [
            "podman", "run", "-i", "--rm",
            "--user", f"{uid}:{gid}",
            "--userns=keep-id",
            "-e", "GIT_CONFIG_COUNT=1",
            "-e", "GIT_CONFIG_KEY_0=safe.directory",
            "-e", "GIT_CONFIG_VALUE_0=/repo",
            "-v", f"{clone_dir}:/repo:ro,z",
            "-v", f"{output_tmp}:/reports:z",
            *extra_mounts,
            "ghcr.io/ambient-code/agentready:latest",
            "assess", "/repo", "--output-dir", "/reports",
        ]
        if config_path_in_container:
            cmd += ["--config", config_path_in_container]

        try:
            subprocess.run(
                cmd,
                input="y\n",
                text=True,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=600,
            )
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "")[:500]
            return AssessmentResult(repo=repo, status="failed", category="container_failure",
                                    message=stderr, output_path="")
        except subprocess.TimeoutExpired:
            return AssessmentResult(repo=repo, status="failed", category="container_failure",
                                    message="podman run timed out after 600s", output_path="")
        finally:
            if tmp_adr_clone:
                tmp_adr_clone.cleanup()

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
        score = _read_overall_score(dest_json)

        symlink = repo_submissions_dir / "assessment-latest.json"
        if symlink.exists() or symlink.is_symlink():
            symlink.unlink()
        symlink.symlink_to(src_json.name)

        return AssessmentResult(repo=repo, status="succeeded", category="",
                                message="", output_path=str(dest_json),
                                is_new=is_new, score=score)


def run_batch(
    org: str,
    repos: List[str],
    output_dir: Path,
    workers: int,
    retries: int,
    default_config: Optional[dict] = None,
) -> Tuple[List[str], List[str], List[str], List[AssessmentResult]]:
    """
    Run assessments concurrently.
    Returns (succeeded, failed, inaccessible, all_results).

    If `default_config` has an `adr_source`, its repo is cloned once here and
    shared across all workers (repos with their own differing `adr_source`
    clone separately inside `assess_repo`). Cleaned up after the batch.
    """
    succeeded = []
    inaccessible = []
    failed = list(repos)
    all_results: List[AssessmentResult] = []

    adr_clone_dir_default: Optional[Path] = None
    adr_clone_tmp: Optional[str] = None
    if default_config and default_config.get("adr_source"):
        adr_repo = default_config["adr_source"]["repo"]
        adr_clone_tmp = tempfile.mkdtemp(prefix="agentready-adr-")
        try:
            subprocess.run(
                ["git", "clone", "--depth=1", f"https://github.com/{adr_repo}.git", adr_clone_tmp],
                check=True, capture_output=True, timeout=120,
            )
            adr_clone_dir_default = Path(adr_clone_tmp)
        except Exception as e:
            print(f"WARNING: default ADR repo clone failed ({adr_repo}): {e} — continuing without adr_source")

    try:
        for attempt in range(retries + 1):
            if not failed:
                break
            if attempt > 0:
                print(f"\nRetry attempt {attempt} for {len(failed)} repos...")

            to_try = list(failed)
            failed = []

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        assess_repo, org, repo, output_dir, default_config, adr_clone_dir_default
                    ): repo
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
    finally:
        if adr_clone_tmp:
            shutil.rmtree(adr_clone_tmp, ignore_errors=True)

    # Deduplicate all_results by keeping only the last result per repo
    seen = {}
    for r in all_results:
        seen[r.repo] = r
    all_results = list(seen.values())

    return succeeded, failed, inaccessible, all_results


def commit_results(repo_root: Path, org: str, repos: List[str]) -> None:
    """Stage and commit all new assessment files in one commit."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    repo_list = ", ".join(repos[:5])
    if len(repos) > 5:
        repo_list += f" (+{len(repos) - 5} more)"

    subprocess.run(
        ["git", "add", "submissions/"],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m",
         f"chore: assess {org} repos {date_str} — {repo_list}"],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(
        ["git", "push"],
        cwd=repo_root,
        check=True,
    )
    print(f"\nCommitted and pushed {len(repos)} assessment(s).")


def write_failed_repos(path: Path, org: str, repos: List[str]) -> None:
    """Write failed repos to a YAML file with the same structure as repos.yaml."""
    data = {"org": org, "repos": repos}
    with open(path, "w") as f:
        f.write(f"# Failed repos from {datetime.now(timezone.utc).isoformat()}\n")
        yaml.dump(data, f, default_flow_style=False)


def write_new_repos(path: Path, org: str, repos: List[Tuple[str, Optional[float]]]) -> None:
    """Write newly-scored repos (name + score) to a YAML file."""
    data = {"org": org, "repos": [{"name": name, "score": score} for name, score in repos]}
    with open(path, "w") as f:
        f.write(f"# New repos scored from {datetime.now(timezone.utc).isoformat()}\n")
        yaml.dump(data, f, default_flow_style=False)


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
            entries.append({"org": org, **entry})
    return entries


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
