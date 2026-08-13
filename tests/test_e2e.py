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
            pytest.skip("GH_TOKEN not set — copy env.example to .env, fill in token, run direnv allow")

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

    def test_nonexistent_repo(self, tmp_path):
        """A repo that doesn't exist should be skipped with auth_not_found."""
        result = assess_repo("konflux-ci", "this-repo-does-not-exist-xyz-999", tmp_path)
        assert isinstance(result, AssessmentResult)
        assert result.status == "skipped"
        assert result.category in ("auth_not_found", "auth_forbidden"), \
            f"Expected auth skip, got {result.category}: {result.message}"
