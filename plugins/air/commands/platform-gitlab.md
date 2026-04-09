# GitLab Platform Reference

This is NOT a command — it is a reference document read by the orchestrator when `PLATFORM=gitlab`. Every `gh` command in review.md and learn.md has a GitLab equivalent documented here.

## Critical: glab CLI Differences from gh

1. **`glab repo view` has no `--json` flag.** Use `glab api projects/<path>` instead.
2. **`glab api` has no `--jq` flag.** Pipe output to `jq` instead: `glab api <endpoint> 2>/dev/null | jq -r '<filter>'`
3. **`glab mr view` uses `-F json` not `--json`.** Output is full MR object, use `jq` to extract fields.
4. **`jq` is required** on the system for GitLab support.
5. **GitLab wiki starts empty** (no Home.md unlike GitHub). The `.git` directory check still works — the clone succeeds even for empty wikis.

## Quick Reference: CLI Commands

| Action | GitHub | GitLab |
|---|---|---|
| Get repo path | `gh repo view --json nameWithOwner --jq '.nameWithOwner'` | `glab api "projects/$(echo $REMOTE_PATH \| sed 's\|/\|%2F\|g')" 2>/dev/null \| jq -r '.path_with_namespace'` |
| Detect MR on branch | `gh pr view --json number --jq '.number'` | `glab mr view -F json 2>/dev/null \| jq -r '.iid'` |
| View MR metadata | `gh pr view <N> --json <fields>` | `glab mr view <N> -F json 2>/dev/null \| jq '{iid, title, state, draft, ...}'` |
| Get MR head SHA | `gh pr view <N> --json headRefOid` | `glab mr view <N> -F json 2>/dev/null \| jq -r '.diff_refs.head_sha'` |
| Get MR diff | `gh pr diff <N>` | `glab mr diff <N>` |
| Checkout MR | `gh pr checkout <N>` | `glab mr checkout <N>` |
| Post comment | `gh pr comment <N> --body-file <f>` | `glab mr note <N> -m "$(cat <f>)"` |
| Approve MR | `gh pr review <N> --approve -b "msg"` | `glab mr approve <N>` |
| Request changes | `gh pr review <N> --request-changes -b "msg"` | No equivalent — see Behavioral Differences #1 |
| Get current user | `gh api user --jq '.login'` | `glab api user 2>/dev/null \| jq -r '.username'` |

## API Path Mappings

GitLab API paths use a project ID or URL-encoded path instead of `owner/repo`. Obtain the project ID first:
```bash
PROJECT_ID=$(glab api "projects/$(echo $CURRENT_REPO | sed 's|/|%2F|g')" 2>/dev/null | jq -r '.id')
```

| GitHub API | GitLab API | Notes |
|---|---|---|
| `repos/<owner>/<repo>/issues/<N>/comments` | `projects/$PROJECT_ID/merge_requests/<iid>/notes` | GitHub treats PRs as issues; GitLab MR notes are separate |
| `repos/<owner>/<repo>/pulls/<N>/commits` | `projects/$PROJECT_ID/merge_requests/<iid>/commits` | Same response shape |
| `repos/<owner>/<repo>/pulls?state=closed` | `projects/$PROJECT_ID/merge_requests?state=merged&order_by=updated_at&sort=desc` | Use `state=merged` not `state=closed` |
| `repos/<owner>/<repo>/pulls/<N>/files` | `projects/$PROJECT_ID/merge_requests/<iid>/changes` | Response: `.changes[].new_path` instead of `.[].filename` |
| `repos/<owner>/<repo>/pulls/<N>/comments` | `projects/$PROJECT_ID/merge_requests/<iid>/discussions` | Inline code comments are discussion threads |
| `repos/<owner>/<repo>/compare/<sha1>...<sha2>` | `projects/$PROJECT_ID/repository/compare?from=<sha1>&to=<sha2>` | Response: `.diffs[].new_path` instead of `.files[].filename` |
| `repos/<owner>/<repo>/issues/comments/<id>` PATCH | `projects/$PROJECT_ID/merge_requests/<iid>/notes/<note_id>` PUT | PUT not PATCH |
| `repos/<owner>/<repo>/contents/<path>` | `projects/$PROJECT_ID/repository/files/<url-encoded-path>/raw?ref=<branch>` | Path must be URL-encoded |

## JSON Field Mappings

When using `--json` flags or parsing API responses:

| GitHub field | GitLab field | Notes |
|---|---|---|
| `nameWithOwner` | `path_with_namespace` | Can be nested: `group/subgroup/project` |
| `number` | `iid` | MR internal ID within project |
| `isDraft` | `draft` | Same boolean semantics |
| `headRefOid` | `sha` or `diff_refs.head_sha` | Both return the MR head commit SHA. `sha` is top-level and simpler; `diff_refs.head_sha` is nested but always present. Use `sha` by default. |
| `baseRefName` | `target_branch` | |
| `headRefName` | `source_branch` | |
| `changedFiles` | `changes_count` | |
| `additions` / `deletions` | `changes` (combined) or parse from diff stats | May need separate calculation |
| `author.login` | `author.username` | |
| `.user.login` | `.author.username` | In note/comment responses |
| `files[].filename` | `changes[].new_path` | From MR changes endpoint |
| `files[].additions` | `changes[].diff` | Must be parsed from diff if needed |
| `statusCheckRollup` | (none) | See Behavioral Differences #2 |
| `reviewDecision` | (none) | See Behavioral Differences #3 |
| `commits.totalCount` | (count from commits endpoint) | `glab api .../commits 2>/dev/null \| jq 'length'` |
| `state: "OPEN"` | `state: "opened"` | Different string values |
| `state: "MERGED"` | `state: "merged"` | Same |
| `state: "CLOSED"` | `state: "closed"` | Same |

## URL Pattern Mappings

| Purpose | GitHub | GitLab |
|---|---|---|
| Wiki clone | `https://github.com/<repo>.wiki.git` | `https://<PLATFORM_DOMAIN>/<repo>.wiki.git` |
| Wiki web link | `https://github.com/<repo>/wiki` | `https://<PLATFORM_DOMAIN>/<repo>/-/wikis` |
| File blob link | `https://github.com/<repo>/blob/<sha>/<file>#L<n>` | `https://<PLATFORM_DOMAIN>/<repo>/-/blob/<sha>/<file>#L<n>` |
| MR URL parse | `github.com/<owner>/<repo>/pull/<N>` | `<domain>/<path>/-/merge_requests/<N>` |

Note the `/-/` segment in GitLab URLs. `PLATFORM_DOMAIN` is `gitlab.com` or a self-hosted domain detected from the git remote.

## Behavioral Differences

### 1. No `--request-changes` equivalent

GitLab has no formal "request changes" review state. When blockers are found:
- Post the review comment as an MR note (same as normal)
- Do NOT approve the MR
- The absence of approval + the comment signals changes are needed
- Skip the `gh pr review --request-changes` step entirely on GitLab

### 2. CI/Pipeline Status

GitHub provides `statusCheckRollup` in the batched PR metadata call. GitLab does not.

Fetch separately:
```bash
glab api "projects/$PROJECT_ID/merge_requests/<iid>/pipelines" 2>/dev/null | jq '.[0] | {status, web_url}'
```

Map status values:
- `success` → all checks pass
- `failed` → CI failing (set `CI_FAILURES`)
- `running` / `pending` → CI still running
- `null` / empty → no pipeline configured

For individual job failures:
```bash
glab api "projects/$PROJECT_ID/pipelines/<pipeline_id>/jobs" 2>/dev/null | jq '.[] | select(.status == "failed") | .name'
```

### 3. Review Decision / Approval State

GitHub provides `reviewDecision` (APPROVED / CHANGES_REQUESTED / REVIEW_REQUIRED). GitLab does not.

Fetch separately:
```bash
glab api "projects/$PROJECT_ID/merge_requests/<iid>/approval_state" 2>/dev/null | jq '.rules[].approved'
```

Or check approvals list:
```bash
glab api "projects/$PROJECT_ID/merge_requests/<iid>/approvals" 2>/dev/null | jq '{approved: .approved, approvals_left: .approvals_left}'
```

### 4. Own-MR Guard

GitHub disallows self-approval (API error). GitLab may allow self-approval depending on project settings. Check:
```bash
glab api "projects/$PROJECT_ID/merge_requests/<iid>/approvals" 2>/dev/null | jq '.approved_by[].user.username'
```
Compare against current user. If project allows self-approval, the guard is unnecessary. If project prohibits it, skip the approve step (same as GitHub behavior).

### 5. Issue Comments vs MR Notes

On GitHub, PRs are a subtype of issues — `issues/<N>/comments` works for PR comments. On GitLab, MR notes are a completely separate resource. Every `gh api repos/.../issues/<N>/comments` call becomes `glab api projects/$PROJECT_ID/merge_requests/<iid>/notes`.

The `--jq` filters for finding `## Code Review` comments remain the same — just the endpoint changes.

### 6. Editing Existing Comments

GitHub: `gh api repos/.../issues/comments/<id> --method PATCH -f body="..."`
GitLab: `glab api projects/$PROJECT_ID/merge_requests/<iid>/notes/<note_id> --method PUT -f body="..."`

Note: PUT not PATCH. The note ID comes from the initial notes query (`.id` field in the response).

### 7. Nested Project Paths

GitLab allows `group/subgroup/project` paths. When using the API with a path instead of numeric ID, URL-encode slashes:
```bash
# group/subgroup/project → group%2Fsubgroup%2Fproject
ENCODED_PATH=$(echo "$CURRENT_REPO" | sed 's|/|%2F|g')
glab api "projects/$ENCODED_PATH" 2>/dev/null | jq -r '.id'
```

Alternatively, resolve the numeric project ID once and use it for all subsequent API calls.

### 8. MR URL Parsing (Cross-Repo Detection)

GitHub PR URL: `https://github.com/owner/repo/pull/123`
GitLab MR URL: `https://gitlab.com/group/project/-/merge_requests/123`

Note the `/-/` segment. For nested groups: `https://gitlab.com/group/subgroup/project/-/merge_requests/123`.

Extract the MR number and project path:
```bash
# From: https://gitlab.example.com/group/subgroup/project/-/merge_requests/123
MR_NUMBER=$(echo "$URL" | sed -n 's|.*/-/merge_requests/\([0-9]*\).*|\1|p')
MR_REPO=$(echo "$URL" | sed 's|https://[^/]*/||; s|/-/merge_requests/.*||')
```

## Verification Checklist

Before first use on GitLab, verify:
```bash
glab --version              # Must be installed
glab auth status            # Must be authenticated
jq --version                # Required — glab api has no --jq flag
glab mr view --help         # Verify mr subcommand exists
glab api --help             # Verify api subcommand exists
```

For self-hosted GitLab, ensure `glab` is configured for the correct instance:
```bash
glab auth login --hostname gitlab.company.com
```
