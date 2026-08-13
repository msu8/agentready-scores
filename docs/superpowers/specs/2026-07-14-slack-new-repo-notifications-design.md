# Slack Notification for Newly-Scored Repositories — Design Spec

**Date:** 2026-07-14
**Status:** Approved

---

## Overview

The assessment pipeline currently sends a Slack notification only on failure (`SLACK_NOTIFICATIONS` + `SLACK_WEBHOOK_URL`, gated `if: failure()`). There is no signal — in Slack or in the GitHub Actions run itself — when a repository gets its first-ever successful score.

This spec adds a "new repo scored" notification: when a run produces `assessment-latest.json` for a repo that never had one before, that repo is reported as newly scored. All newly-scored repos from a run are batched into one Slack message and surfaced as GitHub Actions info annotations.

The detection reuses a check `assess_repo()` already performs (whether a prior `assessment-latest.json` exists) and follows the same status-file → aggregation → notify pipeline already established for failure tracking.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                          assess.py                                │
│                                                                    │
│  process_org()                                                    │
│    ├─ run_batch() → list[AssessmentResult]  (is_new, score added) │
│    ├─ write_failed_repos()          (unchanged)                   │
│    ├─ write_error_details()         (unchanged)                   │
│    └─ write_new_repos()             (NEW — new-<org>.yaml)         │
└──────────────────────────────────────────────────────────────────┘
                           │
                    new-<org>.yaml (committed, mirrors failed-<org>.yaml)
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                      collect_summary.py                          │
│                                                                    │
│  main()                                                            │
│    ├─ collect_summary()      (NEW: new_repos_count/new_repos/     │
│    │                           has_new_repos aggregation)          │
│    └─ emit_gha_annotations() (NEW: ::notice:: per new repo)        │
└──────────────────────────────────────────────────────────────────┘
                           │
                    $GITHUB_OUTPUT: has_new_repos, new_repos_count, new_repos
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│              .github/workflows/assess-scheduled.yml                │
│                                                                    │
│  notify-new-repos job (NEW)                                        │
│    if: success() && has_new_repos == 'true'                       │
│         && vars.SLACK_NOTIFICATIONS == 'true'                     │
│    → one batched Slack message via SLACK_WEBHOOK_URL               │
└──────────────────────────────────────────────────────────────────┘
```

**Data flow:** `assess_repo()` records whether the repo had no prior assessment before this run and, on success, its `overall_score`. `process_org()` collects newly-scored successes and writes `new-<org>.yaml`. `collect_summary.py` aggregates all `new-*.yaml` files across orgs into GH Action outputs and emits `::notice::` annotations. `assess-scheduled.yml` adds one job that turns those outputs into a single Slack message.

**What stays unchanged:** `failed-*.yaml`/`inaccessible-*.yaml` format, `errors-*.json`, the existing failure-notify job, and `assess-manual.yml` (no new Slack job added there — see Scope Decisions).

---

## Detection Logic

### `AssessmentResult` gains two fields

```python
@dataclasses.dataclass
class AssessmentResult:
    repo: str
    status: str
    category: str
    message: str
    output_path: str
    is_new: bool = False              # NEW
    score: Optional[float] = None     # NEW
```

Defaults preserve every existing keyword-argument call site.

### `is_new` is an explicit existence check, not a hash-comparison side effect

`assess_repo()` already computes `prior_hash = _prior_commit_hash(existing_latest)` to decide whether to skip an unchanged repo. `prior_hash` is `None` in two distinct cases:

1. `existing_latest` does not exist (genuinely new repo)
2. `existing_latest` exists but fails to parse (corrupted file, schema change)

Only case 1 is "new." `is_new` is therefore computed as `existing_latest.exists()` evaluated *before* the file is (re)written, independent of the hash comparison. A repo whose history got corrupted is reassessed normally but is never misreported as newly scored.

`score` is populated by reading `overall_score` back from the JSON `assess_repo()` just wrote (`data.get("overall_score")` — defensive, in case a future schema allows it to be absent).

Both fields are only meaningful when `status == "succeeded"`; skipped and failed results keep the defaults.

---

## Per-Org Status File

`process_org()` filters `results` for `status == "succeeded" and is_new`. If any exist, `write_new_repos()` writes `runner/status/new-<org>.yaml`:

```yaml
org: my-org
repos:
  - name: repo-a
    score: 42.5
  - name: repo-b
    score: 18.0
```

If empty, a stale file from a previous run is deleted — identical cleanup behavior to `failed-<org>.yaml`/`inaccessible-<org>.yaml`.

---

## Aggregation & Outputs

`collect_summary()` in `runner_lib.py` globs `new-*.yaml` alongside the existing `failed-*.yaml`/`inaccessible-*.yaml` globs and adds to its returned dict:

- `new_repos_count` — total across all orgs in this run
- `new_repos` — display string `"org/repo (score), org/repo (score), ... and N more"`, truncated at 20 entries (same style as `failed_repos`)
- `has_new_repos` — boolean

`collect_summary.py` writes these as three additional `$GITHUB_OUTPUT` lines.

### Annotations

`emit_gha_annotations()` additionally reads `new-*.yaml` and prints one line per repo:

```
::notice title=new_repo::my-org/repo-a: scored 42.5
```

---

## Slack Notification

New job in `.github/workflows/assess-scheduled.yml` only:

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

### Configuration variables

This feature reuses the two existing pieces of GitHub Actions configuration documented in `README.md` under "Notifications" — no new secret or variable is introduced.

| Name | Kind | Purpose |
|---|---|---|
| `SLACK_WEBHOOK_URL` | Repository **secret** | The Slack Incoming Webhook URL that `slackapi/slack-github-action@v2` posts to. Created once, in Slack, by adding an Incoming Webhook integration to the target channel; the URL itself is the credential (anyone with it can post to that channel), which is why it's a secret rather than a variable. It is channel-specific — whichever channel the webhook was created for is where *every* Slack message from this repo lands, both failure alerts and (with this change) new-repo announcements, since both jobs reference the same `secrets.SLACK_WEBHOOK_URL`. |
| `SLACK_NOTIFICATIONS` | Repository **variable** | A plain (non-secret) on/off flag, set to the literal string `'true'` to enable Slack notifications at all. It's a variable rather than a secret because it carries no sensitive value — it's config, not a credential. Referenced in workflow `if:` conditions as `vars.SLACK_NOTIFICATIONS == 'true'`. |

**Decision: share the toggle.** `notify-new-repos` keys off the same `SLACK_NOTIFICATIONS` variable as the existing failure-notify job — turning Slack on enables both failure alerts and new-repo announcements together, and turning it off disables both. There is deliberately no independent per-notification-type switch. This was chosen over adding a second variable (e.g. `SLACK_NEW_REPO_NOTIFICATIONS`) to keep configuration surface at zero for this change — anyone who already has failure notifications turned on gets new-repo announcements automatically, with nothing to (re)configure.

Both are configured today in the target repo's **Settings → Secrets and variables → Actions** (secrets and variables are separate tabs there).

---

## Scope Decisions

- **Detection runs unconditionally in shared code** (`process_org`), so `assess-manual.yml` runs will still produce `new-<org>.yaml` and a `has_new_repos` output. Only `assess-scheduled.yml` has a job that acts on it — manual runs never post to Slack.
- **GHA info annotations fire on both workflows**, since `emit_gha_annotations()` runs unconditionally in the shared "Collect result summary" step used by both. This is intentional: annotations are low-noise and in-run-only, unlike a Slack message reaching a shared channel.
- **`SLACK_NOTIFICATIONS` is shared, not split per notification type** — see Configuration variables above.

---

## Testing

- `tests/test_runner_lib.py`:
  - New repo (no prior `assessment-latest.json`), succeeds → `is_new is True`, `score` matches the written JSON's `overall_score`.
  - Existing repo, new commit, succeeds → `is_new is False`.
  - Existing but corrupted `assessment-latest.json` → `is_new is False` (file exists, even though hash comparison fails and the repo is reassessed).
  - `write_new_repos()` — writes expected YAML shape; deletes stale file when there are no new repos.
  - `collect_summary()` — single org, multiple orgs, and zero new-repo files, mirroring the existing `test_failures_only`/`test_multiple_orgs`/`test_failed_repos_truncated_at_20` style for the new fields.
  - `emit_gha_annotations()` — one `::notice::` per new repo; no annotation when there are no `new-*.yaml` files.
- `tests/test_collect_summary.py`: extend `$GITHUB_OUTPUT`-writing tests to assert `new_repos_count`/`new_repos`/`has_new_repos` lines, mirroring the existing failure-annotation tests.
- `tests/test_e2e.py`: extend `test_successful_assessment` (real `konflux-ci/build-definitions` call against a fresh `tmp_path`) to also assert `result.is_new is True` and `result.score is not None`.
- No automated workflow-YAML test exists beyond `test.yml` running `pytest tests/`; workflow changes are verified by manual YAML review, consistent with prior workflow changes in this repo.

---

## Files Changed

| File | Change |
|---|---|
| `runner/runner_lib.py` | Add `is_new`/`score` to `AssessmentResult`. Modify `assess_repo()` to compute both. Add `write_new_repos()`. Extend `collect_summary()` and `emit_gha_annotations()`. |
| `runner/assess.py` | Call `write_new_repos()` in `process_org()`, mirroring `write_failed_repos()`. |
| `runner/collect_summary.py` | Write `new_repos_count`/`new_repos`/`has_new_repos` to `$GITHUB_OUTPUT`. |
| `.github/workflows/assess-scheduled.yml` | Add `notify-new-repos` job. |
| `tests/test_runner_lib.py` | Add tests for `is_new`/`score` detection, `write_new_repos()`, aggregation, annotations. |
| `tests/test_collect_summary.py` | Add tests for new GH outputs. |
| `tests/test_e2e.py` | Extend `test_successful_assessment` with `is_new`/`score` assertions. |
| `README.md` | Expand the "Notifications" section to describe the new-repo-scored notification and note that it shares the `SLACK_NOTIFICATIONS`/`SLACK_WEBHOOK_URL` toggle with failure notifications. |

**No changes to:**
- `.github/workflows/assess-manual.yml`
- `failed-*.yaml` / `inaccessible-*.yaml` format
- `errors-*.json` format
- Any secrets or repository variables (reuses `SLACK_WEBHOOK_URL` / `SLACK_NOTIFICATIONS`)

---

## Estimated Size

~80 lines of production code changes (dataclass fields, one existence check, one JSON read, one writer function, aggregation/annotation extensions, one workflow job) + ~100 lines of new/extended unit tests. Additive only — no existing behavior is removed.
