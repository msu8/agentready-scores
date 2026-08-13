import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
import yaml
import subprocess
import os
import json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from runner_lib import validate_token, collect_summary, write_new_repos
from collect_summary import emit_gha_annotations


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
        assert result["has_new_repos"] is False
        assert result["new_repos_count"] == 0
        assert result["new_repos"] == ""

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
