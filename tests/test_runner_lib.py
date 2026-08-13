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
        # Simplified test: first call (git clone) succeeds, second call (git rev-parse) succeeds,
        # third call (podman run) fails
        call_count = {"n": 0}

        def mock_run(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # git clone succeeds
                return MagicMock(returncode=0)
            if call_count["n"] == 2:
                # git rev-parse HEAD succeeds
                m = MagicMock()
                m.returncode = 0
                m.stdout = b"abc123\n"
                return m
            # podman run fails — text=True means stderr is a string
            raise subprocess.CalledProcessError(1, "podman", stderr="container crashed\n")

        with patch("runner_lib.check_repo_access", return_value="accessible"), \
             patch("runner_lib.subprocess.run", side_effect=mock_run), \
             patch("runner_lib.subprocess.check_output", return_value=b"1000\n"), \
             patch("runner_lib._prior_commit_hash", return_value=None):
            result = assess_repo("myorg", "crash-repo", tmp_path)
        assert result.status == "failed"
        assert result.category == "container_failure"
        assert "container crashed" in result.message

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

    def test_retry_deduplicates_results(self, tmp_path):
        """When a repo fails then succeeds on retry, only the success result should appear in all_results."""
        call_count = {"flaky": 0}

        def side_effect(org, repo, out):
            call_count["flaky"] += 1
            if call_count["flaky"] == 1:
                return AssessmentResult(repo=repo, status="failed", category="clone_failure",
                                       message="network blip", output_path="")
            return AssessmentResult(repo=repo, status="succeeded", category="",
                                   message="", output_path=str(tmp_path / "result.json"))

        with patch("runner_lib.assess_repo", side_effect=side_effect):
            succeeded, failed, _, all_results = run_batch(
                org="myorg",
                repos=["flaky"],
                output_dir=tmp_path,
                workers=1,
                retries=1,
            )

            assert succeeded == ["flaky"]
            assert failed == []
            # Only the final success result should appear in all_results
            assert len(all_results) == 1
            assert all_results[0].status == "succeeded"
            assert all_results[0].repo == "flaky"


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
