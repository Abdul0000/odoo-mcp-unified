# Oracle task execution and persistent coding tasks

This optional lane adds isolated task clones, file operations, asynchronous commands,
commit/push, and a persistent OpenAI Responses API coding loop. It is disabled by
default. Existing Odoo authentication and deployment policies are unchanged.

## Runtime boundary

The HTTP API creates work in `ODOO_AGENT_EXECUTION_ROOT` (default
`/var/lib/odoo-mcp/execution`). SQLite WAL transactions handle idempotent submission,
bounded queues and atomic claims. One separate `odoo-executor` systemd service owns
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
is pending in that workspace. The worker serializes execution on the small VM.

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
   separate local clone and `codex/task-*` branch at the checkout's current SHA.
   No implicit pull occurs; check source freshness before starting.
3. `execution_files` supports list/read/search/write/diff. Reuse the returned file
   SHA for replacement. Untracked files appear in workspace status, outside Git diff.
4. `execution_process_start(workspace_id, argv, request_id)` returns a process ID.
   Poll `execution_process_poll` using byte `next_offset`; send bounded input or
   close stdin with `execution_process_input`; stop via `execution_process_cancel`.
5. Review test exit codes and workspace changes. `execution_publish` commits and
   optionally pushes to the task branch or the client's configured deployment branch.
   Existing approval policy applies. Push uses no force and verifies remote SHA.
6. Run the existing Odoo deployment/upgrade verification workflow with that SHA.
   A commit or push is never reported as a confirmed deployment.

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
under an operator-managed retention policy. Do not delete the live SQLite files.

Once installed, deployment rebuilds the cached image and runs the full regression
suite inside it before activating a release. Deployment restarts an active worker
with each release. Lint and shell syntax checks are run before publishing changes.
