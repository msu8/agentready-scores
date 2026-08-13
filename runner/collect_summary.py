#!/usr/bin/env python3
"""Post-assessment CI utilities: token validation and result summary."""
import argparse
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from runner_lib import validate_token, collect_summary, load_error_details, load_new_repos


def write_github_output(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as f:
        for line in lines:
            f.write(line + "\n")


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--validate-token", action="store_true",
        help="Validate GH_TOKEN and exit",
    )
    parser.add_argument(
        "--runner-dir", type=Path, default=SCRIPT_DIR / "status",
        help="Directory containing failed-*.yaml and inaccessible-*.yaml",
    )
    args = parser.parse_args()

    if args.validate_token:
        if validate_token():
            print("GH_TOKEN is valid.")
            return 0
        return 1

    summary = collect_summary(args.runner_dir)
    emit_gha_annotations(args.runner_dir)

    if summary["inaccessible_count"] > 0:
        print(f"Inaccessible repos (token lacks access): {summary['inaccessible_count']}")
        for f in summary["inaccessible_files"]:
            print(open(f).read())

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

    if summary["has_failures"]:
        print(f"Some repos failed assessment ({summary['failed_count']}):")
        for f in summary["failed_files"]:
            print(open(f).read())
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
