# Odoo MCP Agent Connector — Full Backend Plan

This package implements the backend-only connector from the final plan:

```text
Any MCP-capable agent = AI brain
        ↓
OpenAPI Actions / lightweight MCP JSON-RPC bridge
        ↓
FastAPI Odoo Agent Server = tools only
        ↓
GitHub + Odoo.sh + Odoo shell + optional external CDP browser
```

No Codex, no CLI agent, no backend AI model, and no frontend UI are required. The connected MCP agent writes and reasons about code; this server performs the allowed operations.

## Implemented components

### Core API

- `GET /health`
- `GET /openapi.json`
- `GET /docs`
- `GET /tools/list`
- `POST /tools/call`
- `POST /mcp`

`tools/list` (both `/tools/list` metadata and the MCP `tools/list` JSON-RPC
method) includes an `outputSchema` alongside `inputSchema` for every tool
whose result is backed by a pydantic model. This is derived statically —
no fabricated schemas — from each tool's actual return type, so a calling
model knows the shape of a result before parsing it. Tools that return a
plain dict have no `outputSchema` entry, which is valid per the MCP spec.

### Self verification and workflow

- `POST /qa/self-run`
- `GET /workflow/capability-check`
- `POST /workflow/full-verify`

### Repository tools

- `POST /repo/list`
- `POST /repo/read`
- `POST /repo/write`
- `POST /repo/write-many-smart`
- `POST /repo/write-many-fast-safe`
- `POST /repo/write-many-atomic`
- `POST /repo/apply-patch-atomic`
- `POST /repo/delete`
- `POST /repo/apply-patch`
- `POST /repo/clean-pyc`

### Git tools

- `POST /git/status`
- `POST /git/current-branch`
- `POST /git/diff`
- `POST /git/commit`
- `POST /git/push`

`/git/push` defaults to the current branch and pushes `HEAD:<current_branch>` to avoid wrong-branch pushes. Protected branches such as `main`, `master`, `production`, `prod`, and `live` always require a real single-use `git.push` approval, even if global approval mode is disabled.

### Odoo.sh tools

- `POST /odoosh/health`
- `POST /odoosh/git-status`
- `POST /odoosh/log`
- `POST /odoosh/grep-errors`
- `POST /odoosh/module-update`

Odoo.sh deployment is normally triggered by GitHub push. The Odoo.sh tools verify and troubleshoot the build through SSH/logs.

### Odoo shell tools

- `POST /odoo/health`
- `POST /odoo/module-status`
- `POST /odoo/module-install`
- `POST /odoo/module-update`
- `POST /odoo/search-read`

### Browser / CDP tools

- `POST /browser/screenshot`
- `POST /browser/pos-flow`

Navigates Odoo pages through an external Chrome DevTools Protocol browser and returns console/network errors. Screenshots are optional compatibility output; Odoo.sh logs remain the primary server-side diagnostic source.

### Safety and observability

- `POST /approvals/request`
- `POST /approvals/approve`
- `POST /approvals/reject`
- `GET /approvals`
- `GET /jobs`
- `GET /jobs/{job_id}`
- `GET /jobs/audit/events`

High-impact actions require approval by default. Protected branch pushes always require approval regardless of the global setting:

```text
repo.write
repo.delete
repo.apply_patch
repo.clean_pyc
git.commit
git.push
odoo.module_install
odoo.module_update
odoosh.module_update
```

High-impact writes require a one-time approval by default. Read-only discovery and query tools remain approval-free. The default policy covers repository mutations, Git commit/push, module install/update, safe deploy, and sensitive record actions. Set `ODOO_AGENT_REQUIRE_APPROVAL=false` only for an explicitly trusted development environment; production deployments should keep approval enabled.

## Install locally

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env       # Windows: Copy-Item .env.example .env
uvicorn odoo_agent_server.main:app --reload --port 8080
```

Open API docs:

```text
http://localhost:8080/docs
```

## Docker

```bash
cp .env.example .env
mkdir -p repos
docker compose up --build
```

Mount your Odoo repo under `./repos` or change the volume and `ODOO_AGENT_ALLOWED_ROOTS`.

## Optional CDP browser setup (operator-provided)

The connector does not bundle Chromium and never launches one itself. Browser
verification is an **operator-provided dependency**: an MCP agent cannot
provision it. For UI console diagnostics, run Chrome/Chromium outside the
connector with remote debugging enabled and set `ODOO_AGENT_CDP_URL`:

```text
ODOO_AGENT_CDP_URL=http://chrome-host:9222
```

Example local Chrome launch (run on the machine that hosts the browser):

```bash
chrome --remote-debugging-port=9222 --headless=new --disable-gpu --no-sandbox
```

When `ODOO_AGENT_CDP_URL` is empty, `browser.screenshot` and `browser.pos_flow`
return a clear "not configured" result. Source, deploy, and upgrade
verification do not depend on the browser.

## Connect an MCP Client

### Option A: OpenAPI Actions

Deploy this server with HTTPS and import:

```text
https://your-domain.com/openapi.json
```

Use Bearer token auth with `ODOO_AGENT_API_TOKEN`.

### Option B: Lightweight MCP JSON-RPC bridge

Use:

```text
POST /mcp
```

Supported JSON-RPC methods:

```text
initialize
tools/list
tools/call
notifications/initialized
```

Example:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "qa_self_run",
    "arguments": {
      "project_path": "/workspace/repos/my-odoo-repo",
      "module_name": "my_custom_module",
      "run_pytest": true,
      "include_git_status": true
    }
  }
}
```

### Option C: REST tool-call bridge

Use:

```text
GET  /tools/list
POST /tools/call
```

Example:

```json
{
  "name": "qa_self_run",
  "arguments": {
    "project_path": "/workspace/repos/my-odoo-repo",
    "module_name": "my_custom_module",
    "run_pytest": true,
    "include_git_status": true
  }
}
```

This package includes both OpenAPI/Actions and a lightweight MCP JSON-RPC bridge. The standard endpoint is `/mcp`; compatibility profiles also exist for clients with stricter transport or auth behavior.

## Example full verify

```bash
curl -X POST http://localhost:8080/workflow/full-verify \
  -H "Authorization: Bearer change-me-to-a-long-random-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "project_path": "/workspace/repos/my-odoo-repo",
    "module_name": "my_custom_module",
    "run_pytest": true,
    "include_git_status": true,
    "include_git_diff": true,
    "include_odoosh_errors": true,
    "include_odoo_module_status": true,
    "take_browser_screenshot": true,
    "browser_start_path": "/web"
  }'
```

## Approval-free tool execution

This build removes approval blocking for MCP tools. Do not pass `approval_id`; listed tools such as `repo_apply_patch_atomic`, `git_commit`, `git_push`, `odoo_module_deploy`, `odoosh_module_update`, and `workflow_safe_deploy_module` execute directly when the API token and path guardrails allow the request.

## Production rules

1. Keep `ODOO_AGENT_ALLOWED_ROOTS` strict.
2. Use staging/development Odoo.sh first.
3. Keep `ODOO_AGENT_ALLOWED_ROOTS` and target branch/repo settings strict because approval gating is disabled.
4. Store GitHub/Odoo.sh SSH keys only on this backend server.
5. Do not expose Odoo passwords, SSH keys, API keys, or cookies to the MCP client.
6. Push only current branch unless a branch is explicitly provided.
7. Avoid adding production branches to target settings unless you intentionally want direct deploy/push access.
8. Review `GET /jobs/audit/events` regularly.

## Self-verification included in this build

The package includes tests for:

- health/root/OpenAPI readiness
- tool registry
- capability check
- Odoo module static QA
- repo read/write/path escape protection
- `.pyc` cleanup
- git status/diff
- Odoo.sh config error handling
- Odoo shell missing config handling
- approval blocking and approval consumption
- `/tools/call` bridge
- `/workflow/full-verify`

Expected result after packaging:

```text
18 passed
```


## Hard verification additions

The latest verification pass adds regression coverage for:

```text
invalid auth rejection
symlink/path escape blocking
patch check-only non-mutation
unsafe patch path rejection
single-use approvals
approval-free direct push/deploy execution
runtime tool approval descriptors
/mcp initialize + tools/list + tools/call
failed full-verify job/audit persistence
```

Protected branches such as `main`, `master`, `production`, `prod`, and `live` always require a real approved `git.push` approval record, even when global approval gating is disabled.

## Full MCP conversion

All operational REST APIs are also exposed as MCP-callable tools through `POST /mcp` using JSON-RPC `tools/list` and `tools/call`.
Tool lists expose strict underscore names such as `qa_self_run`, `git_push`, and `odoo_module_deploy`; `tools/call` still accepts the older dotted aliases such as `qa.self_run` for compatibility.

The authoritative tool list is always `GET /tools/list` or MCP `tools/list`;
do not rely on a hardcoded count. A representative operational subset:

```text
connector.health
workflow.capability_check
workflow.full_verify
workflow.odoo_code_change
workflow.odoo_debug_and_fix
workflow.safe_deploy_module
qa_self_run
repo.list
repo.read
repo.write
repo.delete
repo_apply_patch_atomic
repo.clean_pyc
git.status
git.current_branch
git.diff
git.commit
git.push
odoosh.health
odoosh.git_status
odoosh.log
odoosh.grep_errors
odoosh.module_update
odoo.health
odoo.module_status
odoo.module_install
odoo.module_update
odoo.search_read
odoo.read_group
odoo.fields_get
odoo.list_models
browser.screenshot
approvals.list
approvals.request
jobs.list
jobs.get  # waits server-side by default until job is done or timeout
audit.list
```


### v10 polling/wait behavior

Long install/upgrade commands still run in background jobs, but `odoo.module_deploy`, `odoo.module_install`, `odoo.module_update`, direct Odoo.sh SSH module tools, and `jobs.get` now wait server-side by default for a short safe window. This avoids fragile repeated client-side polling and prevents Render HTTP timeouts. `workflow.safe_deploy_module` performs only one non-blocking Odoo.sh commit check; if the SHA is not deployed yet, it returns `status=retry_later` with `next_call={"tool":"workflow.resume_job"}` instead of blocking the request.

Recommended call:

```json
{"module_name":"sample_custom_module","wait_for_result":true,"wait_timeout_seconds":20}
```

If the job is still running, the response contains `job_id`; call `jobs.get` with the same `job_id`. `jobs.get` also waits by default.

`/tools/list` and MCP `tools/list` now expose the same operational tool surface with strict underscore tool names. `/mcp` also returns proper per-tool input schemas generated from the Pydantic request models instead of a generic object schema.

Those Pydantic schemas belong only to the MCP connector API. Generated Odoo addon Python code must stay plain Odoo ORM code (`models.Model`, `fields`, `api`) and must not import or use Pydantic.

The following REST endpoints are intentionally not exposed as MCP tools because they are protocol/documentation endpoints, not operational tools:

```text
/docs
/redoc
/openapi.json
/tools/list
/tools/call
/mcp
```

Use MCP `tools/list` instead of `/tools/list`, and MCP `tools/call` instead of `/tools/call` when connecting through MCP.

## MCP OAuth Setup

This package supports OAuth 2.1 / PKCE for MCP clients, in addition to legacy Bearer token auth used by OpenAPI/Actions integrations.

### New OAuth/MCP endpoints

```text
/.well-known/oauth-protected-resource
/.well-known/oauth-protected-resource/mcp
/.well-known/oauth-authorization-server
/.well-known/openid-configuration
/oauth/register
/oauth/authorize
/oauth/token
/mcp
```

### Recommended MCP setup

1. Keep the connector running behind HTTPS, for example with Cloudflare Tunnel:

```powershell
cloudflared tunnel --url http://localhost:8080
```

2. Set the public base URL in `.env` so discovery metadata is stable:

```env
ODOO_AGENT_PUBLIC_BASE_URL=https://your-current-tunnel.trycloudflare.com
```

3. Keep OAuth manual approval enabled for public tunnels:

```env
ODOO_AGENT_OAUTH_AUTO_APPROVE=false
```

4. Restart the FastAPI server after changing `.env`.

5. In your MCP client setup, use:

```text
MCP URL: https://your-current-tunnel.trycloudflare.com/mcp
Authentication: OAuth
Registration method: Dynamic Client Registration
```

The connector advertises `/oauth/register`, `/oauth/authorize`, and `/oauth/token`. During authorization, the browser will show a small consent form. Paste your `ODOO_AGENT_API_TOKEN` into that form to approve the MCP client. The connector then issues short-lived OAuth access tokens and refresh tokens.

### Static OAuth client fallback

If your MCP client asks for a user-defined OAuth client, use:

```env
ODOO_AGENT_OAUTH_STATIC_CLIENT_ID=odoo-agent-mcp
ODOO_AGENT_OAUTH_STATIC_CLIENT_SECRET=
```

Then enter this client ID in the MCP client. Leave client secret empty unless you explicitly set `ODOO_AGENT_OAUTH_STATIC_CLIENT_SECRET`.

### Claude Code quick connect

Claude Code connects to remote MCP servers over HTTP and accepts either a bearer token or an OAuth 2.1 flow. The simplest setup for a private connector is a static bearer token:

```bash
# Claude Code CLI: register the connector with its bearer token
claude mcp add --transport http \
  --header "Authorization: Bearer $ODOO_AGENT_API_TOKEN" \
  odoo-mcp https://your-connector.example.com/mcp
```

Equivalent `.mcp.json` entry:

```json
{
  "mcpServers": {
    "odoo-mcp": {
      "type": "http",
      "url": "https://your-connector.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${ODOO_AGENT_API_TOKEN}"
      }
    }
  }
}
```

If you prefer OAuth instead of a pasted token, native clients use a loopback
redirect (`http://localhost:<port>/callback`). The connector allows loopback
redirects by default and can auto-approve them for headless clients:

```env
ODOO_AGENT_OAUTH_ALLOW_LOOPBACK_REDIRECTS=true
ODOO_AGENT_OAUTH_AUTO_APPROVE_LOOPBACK=true
```

Set `ODOO_AGENT_OAUTH_AUTO_APPROVE_LOOPBACK=false` for shared or public
deployments so a human must paste the admin token on the consent page.

#### Lean tool manifest for tight contexts

Claude Code loads the whole `tools/list` manifest into its planning context.
If the default manifest is too large, request the lean subset by adding
`?profile=minimal` to the MCP URL (or sending `X-MCP-Profile: minimal`):

```text
https://your-connector.example.com/mcp?profile=minimal
```

The lean profile keeps the server-owned composite workflow, the `ws_*`
workspace lane, polling, skills, artifacts, and live read discovery, and drops
tools only needed for manual REST/debug use.

### Live model discovery

Two read-only tools let an agent confirm the live data model before writing
code:

- `odoo_fields_get` — fields of one model (`model`, `limit`): name, type,
  relation, required, readonly, store.
- `odoo_list_models` — `ir.model` rows filtered by an optional `search`
  substring (`include_transient`, `limit`).

Both use the same Odoo.sh SSH / HTTPS JSON-RPC transport as `odoo_search_read`.

### Security notes

- The old `Authorization: Bearer ODOO_AGENT_API_TOKEN` still works for REST/OpenAPI Actions.
- OAuth access tokens also work for `/mcp`, `/tools/list`, and protected APIs.
- `WWW-Authenticate` now advertises protected-resource metadata when an unauthenticated MCP request is made.
- Approval gating is disabled; write tools like `git_push`, `repo_apply_patch_atomic`, and module updates execute directly after authentication.
- Rotate `ODOO_AGENT_API_TOKEN` if it was pasted anywhere public.

## Safe Odoo.sh deployment workflow

For code-changing work, prefer `workflow.safe_deploy_module` instead of manually chaining patch, commit, push, and module update tools. The workflow enforces the safe order:

1. validate the repo and module path;
2. optionally run module QA;
3. configure repo-local Git author identity from `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL`;
4. validate `TARGET_GIT_REPO` and `TARGET_GIT_BRANCH` before push;
5. commit and push the selected paths;
6. check Odoo.sh once for the pushed SHA and return `retry_later` with `workflow.resume_job` if it is not deployed yet;
7. verify the deployed module manifest exists and has `installable: True`;
8. call the background `odoo.module_deploy` path with a short wait window so install/upgrade continues without blocking;
9. return `deploy_running` with `jobs.get` when the module deploy is still running;
10. run module status and registry smoke checks when deploy completes.

Agents must not stop after editing files with “next steps” or ask the user to run `-u`. After changing code they should continue through verification and `workflow.safe_deploy_module`, returning only when complete, blocked, `retry_later`, or `deploy_running`.

For normal Odoo module-code requests, agents must not create fields or views by direct `ir.model.fields` / `ir.ui.view` ORM writes or a Studio-style shortcut. They must patch the custom module's Python/XML files, verify against Enterprise/Community source, and run the safe deployment workflow. Agents also must not print sample MCP JSON blocks and tell the user/client to execute them; they should call the MCP tools directly.

Recommended production variables:

```env
TARGET_GIT_REPO=git@github.com:Abdul0000/test_18.git
TARGET_GIT_BRANCH=feature_branch
TARGET_PROJECT_PATH=/workspace/repos/odoo_project
GIT_AUTHOR_NAME=Odoo MCP Agent
GIT_AUTHOR_EMAIL=odoo-mcp@example.local
ODOOSH_ODOO_PATH=/home/odoo/src/odoo
ODOOSH_USER_ADDONS_PATH=/home/odoo/src/user
ODOOSH_WAIT_TIMEOUT_SECONDS=600
ODOOSH_WAIT_INTERVAL_SECONDS=10
```

## Repo-first MCP coding workflow

For every coding task, the MCP agent should start with the repository tools, not with Odoo database reads:

```text
repo_prepare → repo_list_custom_modules → repo_read → repo_apply_patch_atomic or repo_write_many_smart → git_diff → qa_self_run → workflow_verify_against_core(changed files only) → workflow_safe_deploy_module → workflow_resume_job/jobs_get only when returned as next_call
```

`workflow.odoo_code_change` owns this sequence as a persistent coding session. It resolves the exact repo-relative addon root (including nested addon folders), creates a content-bound module checkpoint once, and returns compact focused write context plus `coding_session_id` and `workspace_session_id`. Continuations reuse the same validated module index, reread exact write targets, and apply one rollback-safe smart batch. Up to five modules stay warm per checkout. Different workspaces no longer share a snapshot lock; post-push cache maintenance is asynchronous and refreshes only modules touched by the pushed commit. Aggregate workspace bytes, artifacts, and terminal jobs are retained under configurable limits. QA, exact-diff core verification, Git, deployment, and module upgrade remain server-owned; safe deploy reuses prior verification only when HEAD and the complete binary working-tree diff still match its opaque proof.

Coding work runs off the MCP event loop with bounded admission. `ODOO_AGENT_CODING_WORKFLOW_MAX_WORKERS` controls active workflows and `ODOO_AGENT_CODING_WORKFLOW_MAX_QUEUE` controls accepted waiting calls; excess calls return `workflow_busy` before protected work starts and can be retried after the returned delay.

If another process changes Git HEAD or module files, the session returns `coding_session_stale` before applying generated writes. Its clean `next_call` starts a fresh inspection, preventing stale-context edits. Bundles are internal checkpoints under the state directory; they are never committed, pushed, or exposed as arbitrary filesystem paths.

Each repository also receives a durable `workspace_session_id`. It retains at most five independently fingerprinted module contexts, loads a module only when a task requests it, and evicts the least recently used module when full. A task's `coding_session_id` remains strict to that exact task/module; the workspace ID may be carried to a different module in the same checkout. Writes immediately resnapshot only the active module and invalidate its cached analysis. After a successful Git push, unchanged cached modules are rebound to the new HEAD without rereading, while changed modules are resnapshotted and invalidated. Workspace cache maintenance is best-effort metadata and cannot block Git, QA, deployment, or upgrade.

New MCP/REST tools:

- `repo.prepare`: clones the target repo if missing, otherwise runs `git fetch`, `git checkout <branch>`, and `git pull --ff-only origin <branch>`. Defaults come from `TARGET_GIT_REPO`, `TARGET_GIT_BRANCH`, `TARGET_PROJECT_PATH`, and `WORKSPACE_ROOT`.
- `repo.list_custom_modules`: scans the checkout for Odoo custom modules using `__manifest__.py`, instead of relying only on installed modules in Odoo.
- `repo.write_many_smart`: preferred multi-file writer; auto-selects fast-safe for small edits and atomic worktree for larger/riskier edits. Optional `delete_paths` join writes and deletes in the same transaction.
- `repo.write_many_fast_safe`: small multi-file writer with path validation, repo lock, backups, temporary replacement, and rollback; no git worktree overhead.
- `repo.write_many_atomic`: large/risky multi-file writer; writes many files as one all-or-none temp worktree transaction; the real checkout is touched only after the full generated diff validates.
- `repo.apply_patch_atomic`: validates and applies a multi-file patch in a temporary worktree/branch, then validates the resulting diff against the real checkout before applying it.
- `workflow.coding_task_plan`: returns the canonical ordered tool plan and guardrails for MCP agents before patching code.

Recommended environment:

```env
WORKSPACE_ROOT=/workspace/repos
ODOO_AGENT_API_TOKEN=<strong-random-secret>
ODOO_AGENT_CONTINUATION_TOKEN_SECRET=<stable-random-secret>
ODOO_AGENT_CONTINUATION_TOKEN_TTL_SECONDS=2592000
ODOO_AGENT_BACKGROUND_MAX_WORKERS=2
ODOO_AGENT_WATCHDOG_ENABLED=true
ODOO_AGENT_WATCHDOG_MODE=safe_redirect
ODOO_AGENT_FAST_WRITE_MAX_FILES=50
ODOO_AGENT_FAST_WRITE_MAX_BYTES_EACH=500000
ODOO_AGENT_FAST_WRITE_MAX_TOTAL_BYTES=5000000
ODOO_AGENT_CODING_SESSION_TTL_SECONDS=3600
ODOO_AGENT_CODING_SESSION_MAX_TOTAL_BYTES=250000000
ODOO_AGENT_CODING_WORKSPACE_MAX_MODULES=5
ODOO_AGENT_CODING_WORKSPACE_MAX_TOTAL_BYTES=500000000
ODOO_AGENT_CODING_WORKFLOW_MAX_WORKERS=2
ODOO_AGENT_CODING_WORKFLOW_MAX_QUEUE=2
ODOO_AGENT_JOB_MAX_COUNT=500
ODOO_AGENT_ARTIFACT_TTL_SECONDS=604800
ODOO_AGENT_ARTIFACT_MAX_TOTAL_BYTES=250000000
TARGET_GIT_REPO=git@github.com:Abdul0000/test_18.git
TARGET_GIT_BRANCH=feature_branch
TARGET_PROJECT_PATH=/workspace/repos/odoo_project
GIT_SSH_KEY_PATH=/tmp/keys/github_key
```

`ODOO_AGENT_OPERATION_GUARD_ENABLED=false` preserves the current MCP flow. Enable it only after validating the stricter duplicate/phase guard with your clients.

The phase-aware watchdog is independent of the optional operation guard. It defaults to `safe_redirect`, so explicitly allowlisted read-only provider calls receive a runtime budget and provider `jobs.get` arguments expose their effective bounded wait. A persisted phase that exceeds the default 120-second soft deadline escalates on the first poll: read-only work switches strategy, while live coding/write/Git/deploy/upgrade work returns an urgent non-blocking poll instruction without unsafe cancellation or duplicate submission. Expired code-worker leases use the existing checkpoint recovery. Set the mode to `shadow` only when observation without intervention is required.

On ephemeral hosts such as Render Free, code-change responses include a signed `resume_token`. The ChatGPT task carries this compact token; after a container restart or missing job, call `workflow_resume_continuation` with the latest token. It restores only the secret-free task contract and always revalidates current source, target ownership, QA, Git, and deployment gates. Keep `ODOO_AGENT_CONTINUATION_TOKEN_SECRET` stable across deploys; it falls back to the stable OAuth token secret or API token when omitted.

Live functional operations are a separate, default-off lane. Set `ODOO_AGENT_FUNCTIONAL_OPERATIONS_ENABLED=true` only after validating the configured Odoo user's ACLs on a test database. `odoo.flow_test`, `odoo.record_create`, `odoo.record_write`, and `odoo.record_action` use authenticated HTTPS RPC with field/action policy checks, idempotent request IDs, read-back verification, and mutation audit; they never fall back to SSH superuser writes or enter the code/deploy workflow. Standard recipes are optional fast paths: arbitrary business/custom models can use ordered dynamic create/write/action/verify steps with prior-step `$ref` outputs, while security/system models and private/CRUD methods remain blocked.

For complete addon verification, `workflow.module_test` starts one background job that can combine local static QA, Odoo-native tests, same-origin controller/API contracts, and bounded functional flows. Native/API/functional layers require the selected `ODOO_CLIENTS` entry to include `"environment": "test"`; static-only testing remains local. Poll `jobs.get` and accept success only from a terminal result with `verified=true` and `success_allowed=true`. This workflow never edits, commits, pushes, upgrades, or deploys code.

Legacy `ODOO_AGENT_BACKGROUND_MAX_WORKERS=1` values are accepted and safely clamped to two workers internally.

For SSH GitHub repos, either set `GIT_SSH_COMMAND` yourself or set `GIT_SSH_KEY_PATH`; `repo.prepare` will build a safe `GIT_SSH_COMMAND` using that key.

## Explicit verification override

`workflow.odoo_code_change` remains strict by default. For verified static-analysis false positives, callers may set:

```json
{
  "verification_override": "allow_unverified",
  "verification_override_reason": "Custom multi-model XML caused known static resolver ambiguity."
}
```

This mode accepts only configured overrideable static-analysis categories. The default policy includes custom-field resolution ambiguity, related-field resolver ambiguity, and uncertain XPath match counts. Unknown or newly introduced verifier categories remain blocking until classified.

For an explicit user-authorized force push and deploy:

```json
{
  "verification_override": "force",
  "verification_override_confirmation": "I AUTHORIZE FORCE PUSH AND DEPLOY",
  "verification_override_reason": "User reviewed the reported findings and explicitly accepted the deployment risk."
}
```

The force override applies only to `workflow.verify_against_core`. Blocking QA, Python/XML syntax, parser, manifest, and module-structure failures still stop deployment. Override mode, reason, and accepted findings are attached to the safe-deploy request and returned in workflow results for auditability.

## Workspace tools (`ws_*`) — stateless coding lane

A coding loop that needs no checkout and no persistent disk (built for Render free).

| Tool | Purpose |
|---|---|
| `ws_open` | Open a module on the client's GitHub branch; returns `ws_id` + file index. Resumes unfinished work. |
| `ws_read` / `ws_grep` / `ws_diff` | Inspect files (including your uncommitted edits). |
| `ws_edit` | Atomic search/replace or full-file writes. Lints instantly (python, xml, xpath, js, csv, manifest, imports) and auto-wires **new** files (`from . import`, manifest `data`). |
| `ws_outline` | Compact map of the module (models with typed fields, views with inheritance, menus) instead of reading whole files. |
| `ws_scaffold` | Write less: model + field spec in; model class, form/list views (tree vs list by series), action, menu, access rule, wiring and a new module's manifest out. |
| `ws_extend` | Add fields to an existing model via `_inherit`, optionally inheriting a view next to an anchor field. |
| `ws_symbol` | Verify a model/field (type, comodel, compute, defining module) against upstream `odoo/odoo` at the module's series, plus local code. Community modules in `depends` only; enterprise is not covered. |
| `ws_lint` | Re-run lint and module-closure checks; returns `commit_ready`. |
| `ws_commit` | One atomic GitHub commit via the Git Data API (never force). Replays edits onto a newer branch head, retries are idempotent (`MCP-Op` commit trailer). Returns `next_call` for the Odoo.sh deploy proof. |
| `ws_discard` | Drop the workspace and its WIP branch. |

How it survives Render restarts: after each successful `ws_edit` the edit journal is checkpointed to the
GitHub branch `mcp/wip/<client>-<module>`. `ws_id` is self-describing, so any call after a spin-down
rebuilds the workspace by replaying the journal on the current branch head. Conflicts are reported per edit.

Requirements: each client in `ODOO_CLIENTS` needs an HTTPS `github.com` repo and a `token` with
For GitHub, use a token with `contents:write` (or `GITHUB_TOKEN`). GitLab HTTPS/SSH projects are supported through the GitLab v4 API; set a project access token or personal access token with `api` scope in the client's `token` field. SSH URLs are parsed as repository addresses; API access still uses HTTPS and the token. Other providers keep using `workflow_odoo_code_change`.

What `ws_edit` wires for you: new `.py` files get their `from . import` (creating `__init__.py` chains), new
`views|security|data|report|wizard|demo` XML/CSV files are added to the manifest (security first), and *deleting* such a
file removes its import / manifest entry. Static files under `static/src` that no manifest `assets` entry covers raise an
`asset_unregistered` warning (they would never load). Binary files and files over 400 KB are rejected.

Cost model (measured against a fake GitHub): `ws_open` ~7 API calls regardless of module size (files load lazily);
each `ws_edit` ~4-5 calls (only the compacted journal is checkpointed; file bytes upload once, at commit; identical content
is never uploaded twice); a `ws_commit` on an unmoved branch does no tree walk and no trailer lookup. Connections are kept alive.

Settings: `ODOO_AGENT_WS_CHECKPOINT_MODE` (`async` default: edits return immediately, a background thread persists the journal;
`sync` persists before returning), `ODOO_AGENT_WS_PROTECTED_BRANCHES` (default `main,master,production,prod,live`; pushing there is blocked,
use a feature branch), `ODOO_AGENT_WS_AUTOCHECKPOINT`, `ODOO_AGENT_TOON_OUTPUT` (`auto` | `all` | `off`).

`ws_*` do not require the heavy `client_check` Odoo validation (that state lives in `/tmp` and is lost on every
Render spin-down); they only touch GitHub with the client's own token. The deploy proof that follows
(`workflow_module_install_and_check`) keeps its existing validation gate.

### TOON output
Text results of `ws_*` and tabular query tools are rendered as TOON (uniform arrays collapse to a header plus rows;
code and diffs are emitted raw instead of JSON-escaped). The renderer verifies a lossless round-trip and falls back
to compact JSON whenever TOON is not smaller, so it can never grow a response. `structuredContent` stays JSON.

### Running the offline tests
```
python -m unittest tests.test_toon_service tests.test_workspace_service tests.test_gitlab_commit_service tests.test_ws_tool_wiring
```
(stdlib only; they use an in-memory fake of the GitHub Git Data API in `tests/support/fake_github.py`.)

### Scenario harness
`python -m tests.evals.run` runs scripted end-to-end tasks (new model, extend + verify, lint-fix loop, delete, teammate push
mid-task, restart mid-task) against in-memory fakes and enforces tool-call and API-call budgets. It evaluates the *tooling*;
it does not measure a language model. Run the same task prompts through the live connector to evaluate a model.

### Where do my changes show up on GitHub?
1. While editing: on the branch `mcp/wip/<client>-<module>` (real files, plus `.mcp-wip-*.json`). They are **not** on the client branch yet.
   Switch the branch dropdown in GitHub to that branch. Disable with `ODOO_AGENT_WS_WIP_FILES=false`; changesets over 60 files / 2 MB are journal-only.
2. After `ws_commit`: on the client's configured branch (for example `staging`), which is often **not** GitHub's default branch (`main`).
   The result contains `urls.commit` and a `where` note naming the branch. `ws_status` answers "where is my work?" at any time.
3. If nothing appears anywhere: check `checkpoint.saved` / the `ws_commit` status (blocked_by_lint, blocked_protected_branch,
   conflict, or a GitHub 401/403 meaning the token lacks `contents:write`).


## REA — Reverse Engineer Anything (Phase 1)

The read-only `rea_xray_module` MCP tool builds a bounded, evidence-backed
architecture graph for a custom Odoo addon. It reuses the validated client and
module resolution path from `workflow_module_analyze`, then extracts module
dependencies, model definitions and inheritance, relational fields, methods,
views, window actions, menus, and view-to-method links from Python AST and XML.

The X-Ray runs locally against the selected checkout, does not import or
execute addon code, does not contact external hosts, and requires no persistent
disk. Its response includes graph nodes, typed edges with source-path evidence,
coverage/omitted-file information, and parse warnings. Static relationships are
not represented as runtime proof.

External API/TLS investigation, runtime tracing, and upstream version diffs are
later REA phases and are not part of this initial tool.


## REA Phase 2 — Upstream intelligence

`rea_upstream_compare` compares custom addon model extensions against the selected client's installed Odoo source roots and declared manifest dependencies. It reports method-signature drift, candidate overrides with no same-named upstream method, and field type/comodel differences with custom and upstream file evidence. Analysis is read-only and bounded. It does not fetch arbitrary code or execute addon code. Version/edition is not inferred unless identified by the installed source tree; findings are advisory and require runtime confirmation.


## REA Phase 3 — External API integration intelligence

`rea_api_inspect` statically inventories external integration behavior in a custom addon: HTTP/RPC client calls, webhook/controller routes, literal URL references, environment/config credential references, timeout parameters, and TLS verification settings. Each finding includes file and line evidence plus coverage metadata.

This phase performs no outbound requests, DNS resolution, credential reads, or endpoint probing. It parses source only and reports configuration names rather than secret values. Network/TLS checks require a separate explicitly authorized, allowlisted endpoint workflow.


## REA Phase 4 — Runtime execution tracing

`rea_runtime_trace` analyzes a caller-supplied, bounded export of Odoo runtime spans. It correlates model/method durations, highlights slow calls and high call counts, and groups repeated SQL/query signatures as possible N+1 patterns. Input is capped at 2,000 events and output includes event-index evidence. The tool does not activate Odoo's profiler, attach to a live worker, execute SQL, or capture traffic; profiling/export must be explicitly performed by the operator using an approved environment and supplied to the analyzer.


## REA Phase 5 — Change impact prediction

`rea_impact_predict` predicts likely addon components affected by a proposed change. Supply the addon name and a bounded list of changed addon-relative paths; optional unified diff snippets improve symbol matching. The tool builds the static X-Ray graph, compares the addon with installed upstream source, inspects external API integration points, maps changed paths/symbols to graph nodes, and follows connected model/view/action relationships. Optional caller-supplied runtime trace events add evidence when changed methods appear in the trace. The response includes evidence matches and coverage from each source.

Example MCP arguments:

```json
{
  "module_name": "my_custom_addon",
  "changed_files": [
    {
      "path": "models/sale_order.py",
      "change_type": "modify",
      "diff": "-    def _compute_amount(self):\n+    def _compute_amount(self, discount=None):"
    },
    {
      "path": "views/sale_order_views.xml",
      "change_type": "modify",
      "diff": ""
    }
  ]
}
```

The prediction is static and advisory, not proof of runtime impact. It does not modify files, run tests, query Odoo, deploy, or push. Validate high-risk findings with focused tests and an explicitly approved staging workflow.


## REA Phase 6 — Guarded self-healing workflow

`rea_self_heal` connects REA investigation to the existing Odoo debug-and-fix pipeline. It first resolves one unambiguous custom addon, then gathers bounded X-Ray architecture, installed-upstream compatibility, and external API integration evidence. Those findings are attached to the repair context before the established debugger derives the fault, requests exact writes when needed, and applies the existing verification gates.

Example MCP arguments:

```json
{
  "module_name": "my_custom_addon",
  "issue_description": "The order total is incorrect after applying a fixed discount.",
  "expected_result": "The order total must equal the discounted line totals.",
  "reproduction_steps": [
    "Create a quotation with two order lines",
    "Apply a fixed discount",
    "Recompute the order total"
  ],
  "surface": "backend"
}
```

Optional `runtime_events` and `proposed_changes` inputs activate the Phase 4 trace analyzer and Phase 5 impact predictor; these results are attached to the repair context too. Deployment defaults to **disabled**. Set `auto_deploy=true` only when deployment is explicitly authorized. The workflow does not claim a fix merely because files were written: the existing debug workflow requires its verification evidence before reporting a verified repair. Ambiguous addon resolution blocks the workflow before repair. REA's source scans are static evidence, not proof of runtime behavior.



## REA Phase 8 — Stateless Git workspace analysis

REA X-Ray, upstream comparison, external API inspection, and impact prediction
accept an optional `ws_id` returned by `ws_open`. When supplied, the analyzer
rebuilds the module from the selected GitHub branch and its persisted WIP
journal, so the current unpublished workspace overlay is included. The source
snapshot is bounded (maximum 120 files and 2 MB), materialized only in a
temporary directory for the existing AST/XML analyzers, and removed when the
request completes. No persistent Render checkout is required.

Example sequence:

```json
{
  "module_name": "my_custom_addon",
  "ws_id": "the_ws_id_returned_by_ws_open"
}
```

Use the same `ws_id` with `rea_xray_module`, `rea_api_inspect`,
`rea_upstream_compare`, or `rea_impact_predict`. The `module_name` must match
the module encoded in the workspace ID. If `ws_id` is omitted, the existing
local-checkout behavior remains available.

For workspace upstream comparison, the connector reads the addon manifest's
Odoo series and fetches matching Community source from `odoo/odoo` (for example
`18.0`) through the configured GitHub token. This avoids relying on
`/home/odoo/src` being present on Render. Enterprise/private source remains
out of scope. Phase 9 resolves transitive and reverse dependencies among addons
visible in the same GitHub repository; core, Enterprise, and external-repository
dependencies remain unresolved when their source is not in that repository.

`rea_self_heal` continues to use the existing checkout-backed debug/fix
workflow. The stateless analysis tools do not imply that a repair, push,
deployment, or module upgrade has occurred.



## REA Phase 9 — Transitive dependency intelligence

The stateless `rea_xray_module` response now includes a `rea_dependencies`
section when called with a `ws_id`. It inspects addon manifests in the same
GitHub repository and returns:

- `transitive_dependencies`: bounded breadth-first dependency closure with depth.
- `reverse_dependents`: direct and transitive sibling addons that depend on the target.
- `dependency_cycles` and `cycle_modules`: detected cycles in the visible addon graph.
- `cross_module_relationships`: source-evidenced model inheritance/delegation and relational-field links between addons.
- `model_ownership`: module, model, role, and source location.
- `unresolved_dependencies` and `ambiguous_dependencies`: dependencies not safely resolvable inside the repository.

Traversal is cycle-safe and bounded to at most 100 modules, 240 Python files,
a 2 MB source budget, and depth 12. Coverage reports manifest/file omissions
instead of silently treating a partial scan as complete. The target module's
current WIP overlay is included; sibling addons use the selected branch
snapshot. Core, Enterprise, and external-repository dependencies are not
pretended to be resolved.

The stateless `rea_impact_predict` path now accepts this dependency evidence
internally and reports affected reverse-dependent addons, cross-module links,
and dependency-cycle risk alongside the existing component-level prediction.
These results remain static and advisory, not proof of runtime impact.


## Odoo 19+ External JSON-2 transport

For Odoo 19+ databases that expose External JSON-2, add `odoo_api_key` to the selected `ODOO_CLIENTS` entry (or set `ODOO_API_KEY` for single-client configuration). The connector uses `POST /json/2/<model>/<method>` with `Authorization: bearer <key>` for `search_read`, `read_group`, `fields_get`, `read`, `create`, `write`, access-right checks, and business actions that use named arguments. Recordset methods send `ids`; create sends `vals_list`; write sends `vals`. JSON-2 has no positional-argument support, so actions that require positional arguments return a clear unsupported-arguments error. The transport sends named parameters and `X-Odoo-Database` when configured. When no API key is supplied, existing username/password clients continue using legacy JSON-RPC. API keys are never included in public client metadata. JSON-2 may require an eligible Odoo plan, and each request runs in its own transaction; multi-step business operations requiring atomicity should be implemented as one server-side Odoo method.


**Deployment note:** an existing Render environment variable overrides code defaults. Set `ODOO_AGENT_REQUIRE_APPROVAL=true` in the Render service environment and redeploy to activate the new default on an already-configured service. Existing explicit `false` values remain respected for compatibility.



## REA Phase 10 — Runtime-aware dependency intelligence

rea_xray_module and workspace-backed rea_impact_predict can reconcile static
addon dependency/model evidence with the selected database's live Odoo
registry. Pass an explicit client_name to enable the bounded, read-only
registry check. The connector queries ir.model, ir.model.fields, and
ir.module.module through the existing OdooService transport (SSH-first,
HTTPS JSON-RPC fallback); it never writes records or executes addon code.

The result labels runtime evidence separately from static inference and reports
model presence, Odoo's registered owning modules, relation-field confirmation,
module installation state, query errors, and truncation coverage. A failed or
partial registry query is marked incomplete and is never interpreted as proof
that a model or module is absent. Registry presence confirms metadata only,
not method execution. The scan is bounded to 250 models, 3,000 fields, and 150
modules. Omitting client_name keeps the analysis static-only.
