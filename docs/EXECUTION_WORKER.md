# Oracle task execution and persistent coding tasks

Normal ChatGPT-to-MCP coding needs no OpenAI API key on Oracle: ChatGPT supplies
reasoning, and the Oracle worker executes tools. Optional standalone agent tools
are hidden unless their separate model configuration exists.

This optional lane adds isolated task clones, file operations, asynchronous commands,
commit/push, and a persistent OpenAI Responses API coding loop. It is disabled by
default. Existing Odoo authentication and deployment policies are unchanged.

## Runtime boundary

The HTTP API creates work in `ODOO_AGENT_EXECUTION_ROOT` (default
`/var/lib/odoo-mcp/execution`). Atomic JSON files and cross-process file locks handle
idempotent submission, bounded queues and atomic claims. No database is required.
One separate `odoo-executor` systemd service owns
the worker lock. Only this account joins the Docker group; the API does not need it.
Docker access is privileged: do not expose that socket to the task container.

Each command runs in a disposable container with no network, read-only root and Git
metadata, dropped capabilities, no-new-privileges, one CPU, 384 MB memory and swap
limit, 128 PIDs, and 64 MB temporary storage. Only the task clone is writable.
The host API/Odoo/GitHub/OpenAI environment and SSH directories are not mounted.
Commands cannot fetch packages; build required dependencies into the image. Ordinary
Odoo-native tests still need the existing configured Odoo test environment.

The API and worker share group-writable state using umask 0007. Task commands run
with the task owner's numeric UID/GID. Source checkouts remain unchanged. Existing
files require their current SHA-256 for replacement; file tools reject traversal,
Git metadata and escaping symlinks. File edits/publish are blocked while a command
is pending in that workspace. `ODOO_AGENT_EXECUTION_MAX_WORKERS` bounds parallel
commands across independent tasks (1–4, default 1 for the small VM). A workspace
still permits only one pending command. Budget memory before increasing concurrency.

## Install on the existing OCI deployment

Deploy this source release first, then run:

```sh
sudo bash /opt/odoo-mcp/current/deploy/install-execution.sh
```

It builds the execution image, creates the dedicated worker account, enables the
worker, and restarts the API with execution enabled. No OpenAI key is needed for
ChatGPT-controlled MCP file/command/publish calls. To turn execution off, set
`ODOO_AGENT_EXECUTION_ENABLED=false`, restart the API and stop the worker.

Use the normal `/mcp` endpoint or `/mcp?profile=execution` for a smaller manifest
containing task execution, client selection, module-install verification and jobs.
Refresh the ChatGPT custom MCP connection to discover the added tools.

## Chat-controlled coding

1. Select/prepare the existing client checkout with `client_check`.
2. `execution_workspace_open(client_name, task_description, request_id)` creates a
   separate local clone on the configured branch. It fetches and fast-forwards
   the task clone by default; the source checkout is never moved. Dirty source
   and unpublished/diverged histories are rejected with files preserved. Immutable
   Git objects use local hard links to avoid copying large pack files; worktree
   files/index remain independent and container Git metadata stays read-only. A different branch
   requires an explicit `branch`. `fresh=false` is available for offline inspection;
   publishing to an existing source branch still requires a known remote base.
3. Declare acceptance commands once with `execution_task_plan(workspace_id,
   checks=[{name, argv, cwd, timeout_seconds}], skills=[...])`. Choose relevant
   existing tests/checks for the requested change. Plans cannot be downgraded later.
   Optional skill content is returned once; names and content hashes stay with the
   task. Skills guide the model; the server does not infer semantic correctness.
4. `execution_files` supports list/read/search/write/diff and `batch` edits.
   Existing edits require the read SHA. Batch operations are write, exact single
   text replacement, rename and delete. All paths/hashes are validated first;
   write failures restore original files, and failed rollback quarantines the task.
   This is rollback on caught errors, not a multi-file filesystem transaction
   across power loss. Untracked files are in workspace status, outside Git diff;
   truncated diff/search output is marked explicitly.
5. `execution_process_start(workspace_id, argv, request_id)` returns a process ID.
   Poll `execution_process_poll` using byte `next_offset`; send bounded input or
   close stdin with `execution_process_input`; stop via `execution_process_cancel`.
   To record verification, pass `check_name` and the exact planned argv/cwd/timeout.
   Verification mounts source read-only; put generated test data/caches in `/tmp`.
   It records exit status against the exact prospective Git tree (including new
   files, deletions and modes). Edits invalidate old evidence. An ordinary command
   exit 0 does not count as a planned check. Interrupted rechecks invalidate prior
   success. Failed checks allow investigation/edit/retest up to the task's
   `max_repair_attempts` (default 3); exhaustion requires a new scoped task.
6. `execution_workspace_status` returns persisted phase, missing checks and the
   next call. Phases include inspect/edit/verify/repair/commit/push/done and explicit
   needs_input/needs_review. Each workspace owns its state independently of the
   legacy per-client workflow guard. Repeat searches and reads are allowed during repair.
   `execution_status.recent_tasks` lists recent task IDs for resuming after a new
   chat; its last-known phases are not verification proof. Refresh workspace status.
7. `execution_publish` requires every planned check to pass against current source,
   then commits and optionally pushes. Default target is the configured branch.
   It rejects an advanced remote and never force-pushes or automatically rebases
   tested changes. It verifies both the committed tree and remote SHA. Existing
   approval policy still applies. A successful commit alone is not a pushed task.
8. Run the existing Odoo deployment/upgrade verification workflow with that SHA.
   A commit or push is never reported as a confirmed deployment.

Example plan and verification call:

```json
{"workspace_id":"<id>","checks":[{"name":"regression","argv":["python","-m","pytest","-q","-p","no:cacheprovider"],"cwd":".","timeout_seconds":120}]}
```

```json
{"workspace_id":"<id>","check_name":"regression","argv":["python","-m","pytest","-q","-p","no:cacheprovider"],"cwd":".","timeout_seconds":120,"request_id":"regression-attempt-1"}
```

The image contains generic Python/Node tooling, not a complete Odoo runtime.
Odoo-native checks still require the existing Odoo test/deployment environment;
passing container checks must never be presented as live Odoo functional proof.

Submission request IDs are immutable: same ID and payload returns existing work;
changed payload with the same ID is rejected. For a publish with uncertain outcome,
inspect local/remote SHA before creating another request. Arbitrary host-service
commands are not exposed through the container lane.

## Independent background coding

Configure these in `/etc/odoo-mcp/odoo-mcp.env`, then restart both API and worker:

```dotenv
OPENAI_API_KEY=<operator-provided key>
OPENAI_MODEL=<model supporting Responses API function calls>
# Optional: OPENAI_BASE_URL=https://api.openai.com/v1
```

`execution_agent_start(workspace_id, task, request_id, max_turns=20)` creates a
durable task. The worker reasons via the Responses API, executes source/file/test
tools, and persists response IDs, tool outputs, turn count and token usage after
each turn. Model turns have bounded output and the task has a maximum turn count.
API usage is separately billed. Model configuration values are never returned by
status tools or inherited by command containers.

The agent has only workspace/file/process tools. It cannot approve its own actions,
switch workspaces, access arbitrary services, publish or deploy. ChatGPT reviews its
output and uses the explicit publish/deploy workflow. Poll `execution_agent_status`;
completion is a model summary, not independent evidence that every test passed.
Cancellation also requests cancellation of the agent's active command.

The worker survives browser/chat disconnects. On a worker restart, running commands
are removed and marked interrupted; an agent interrupted during a turn is marked
`needs_review`. Queued checkpoints continue. Unknown side effects are never silently
replayed. Review the workspace and logs, then submit a new bounded task to continue.

## Verification and operations

```sh
python -m pytest -q tests/test_execution_worker.py
ruff check odoo_agent_server deploy
sudo systemctl status odoo-mcp-execution.service
sudo journalctl -u odoo-mcp-execution.service -n 50 --no-pager
```

`execution_status` reports the worker heartbeat and whether a model is configured.
Logs cap at 2 MB per command; hitting the cap stops the container. Queue capacity
defaults to ten outstanding commands and ten agent tasks. There is no automatic
deletion of task workspaces: archive important work and remove old execution data
under an operator-managed retention policy. Do not delete active task records.

Once installed, deployment rebuilds the cached image and runs the full regression
suite inside it before activating a release. Deployment restarts an active worker
with each release. Lint and shell syntax checks are run before publishing changes.
