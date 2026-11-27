# Durable coding, debugging and functional tasks

ChatGPT performs reasoning using the existing MCP connection. No additional model
API key or SQLite is used. The coordinator delegates to existing tools and retains
their approval, client-validation, verification and deployment gates. It cannot
expose provider-hidden tools through an arbitrary tool tunnel.

## Tool flow

1. `task_open(client_name, request_id, description, target, module_name?)` pins the
   client profile and completion target. Map intent exactly: implementation/fix
   without publishing is `coding`; an explicit push is `push`; an explicit deploy
   is `deploy`; live business-record work is `functional`. Never default an
   explicitly requested push/deploy to `coding`, and never promote coding to push
   or deploy without the user's request.
2. Follow `next_call` or generate the technical fields in `argument_request`.
   `task_run(task_id, step_id, tool, arguments)` links ordinary tool results to the
   ledger. Mutations use stable unique step IDs; read-only polls may reuse one.
   The nested `tool_result` preserves the underlying operation's evidence.
3. After edits, `task_test_plan(task_id)` proposes relevant Python/XML syntax,
   matching regression tests and declared frontend test/build scripts. Native
   Odoo tests require the isolated Odoo runtime; missing layers are explicit.
   `apply=true` strengthens the existing plan and cannot remove acceptance checks.
4. Execute named checks and inspect exact-tree/runtime-bound evidence. Publish
   using the existing execution tools, with `push=false` for coding-only tasks.
5. For requested deployment, `task_deploy` adopts the exact verified pushed
   workspace commit. It safely fast-forwards a clean configured checkout, verifies
   client/branch/local/remote SHA, classifies upgrade impact, and delegates to safe
   deploy. Commit and push are skipped; normal source, registry, upgrade and
   task-specific live-assertion gates remain. Changed remote/source blocks adoption.
   Set `require_runtime_tests=true` when this deployment also requires test
   evidence. The worker can supply a passing unit or ORM layer from the adopted
   commit. Standalone QA reports `static_status` and `runtime_test_status`
   separately, including deferred native tests.
6. `task_resume` performs bounded read-only workspace/process/job reconciliation.
   It never replays a commit, push, upgrade or record action. A source match alone
   cannot confirm a module upgrade or business outcome. Task claims describe only
   their declared target; nested push success cannot override a deployment target.

Tasks persist atomically in `ODOO_AGENT_STATE_DIR/tasks`, with at most 200 distinct
steps and 100 timeline events per task. New tasks are capped by `job_max_count`;
capacity exhaustion is explicit. Existing records are not silently deleted.
Back up task state with the connector state directory. A configuration change
invalidates old claims and requires a new task with preserved work inspected.
Task IDs, linked server jobs/workspaces, summaries and fingerprints survive service
restarts. Raw code, shell output, passwords and operation arguments are not copied
into the task ledger; they remain in their existing scoped stores.

## Functional preview and uncertain outcomes

`odoo_mutation_preview(operation=create|write|action, arguments=...)` checks live
metadata/permissions and returns the exact affected records, proposed payload,
10-minute preview ID and write_executed=false. Execute the unchanged operation
with that preview_id. Changed records/profile/payload or expiry blocks dispatch.
This is a read-before-write guard, not a transactional compare-and-swap against
other Odoo sessions. Sensitive action approval is still required.

Functional writes use a per-client/request POSIX lock and durable write-ahead
intent before the RPC. Concurrent duplicate requests serialize. A crash or lost
response after dispatch returns reconciliation_required on retry, rather than
creating duplicate records/actions. Completed per-request checkpoints survive
aggregate audit retention. Configuration/database identity changes block replay.
Exactly-once execution across a remote Odoo transaction is not claimed: uncertain
outcomes must be inspected, not bypassed using a new request ID.

Known business actions check expected states. Custom actions remain callable but
require explicit expected_values for verified functional success; a successful
RPC and read-back alone do not prove the custom business result. Dynamic flow
steps can supply the same postconditions. Live mutations/upgrades in client
production databases are not part of the connector's automated verification.

## Debugging and verification

Existing Odoo debug sessions retain trusted tracebacks, source mapping, bounded
repair cycles and live assertions. Trusted authentication, SSH/network and
missing-dependency evidence is classified separately, preventing blind addon
patches for infrastructure failures. Untrusted narrative cannot authorize repair.

Tests cover real temporary Git editing/testing/publishing, exact-tree invalidation,
client/branch boundaries, provider schemas, crash/replay behavior, concurrent
functional duplicates, preview staleness, custom-action postconditions, maintenance
locks and truthful deployment claims. Browser/business checks still require the
appropriate configured runtime and authenticated client access; unavailable layers
are reported explicitly.

Creation-only CRM opportunity, Project task and Helpdesk ticket recipes extend the
existing Sales/Purchase/Inventory/MRP/Accounting recipes. They validate installed
model fields and user access first and expose no guessed stage-transition methods.
Helpdesk requires the actual installed app. Use dynamic steps plus explicit
postconditions for custom/version-specific transitions.

## Guided workflow for models with limited reasoning

Use this coordinator only when `workflow_agent_instructions.provider_flow` lists
task tools and selects Oracle execution; otherwise follow its configured code
lane directly. Always supply the required target that matches user intent.

Call `task_run(task_id)` to follow one server-selected next step. The server first
reconciles existing operations, then forwards the current scoped next call. It
never repeats an uncertain mutation or invents code, business intent or approvals.
If source-derived test planning supplies every required layer without blockers,
the server installs that plan through the existing guarded tool. Otherwise the
response contains concrete test proposals and missing requirements for the model.

For source reads/edits, supply `tool` and `arguments`. Linked workspace, unambiguous
process/job/operation IDs, check argv, task description, commit message and scoped
request IDs can be supplied by the coordinator. Conflicting explicit scope is
rejected. Coding-only tasks default to local commits; push/deploy authority still
comes from the pinned task target and normal approval policy. Model-selected tests
cannot replace existing acceptance checks.

Use one implementation lane for the whole task. The coordinator follows the
connector's configured provider flow; without the coordinator, use the exact
`provider_flow.code_change` or fallback returned by the connector. Skill examples
do not select or override a lane. If that lane is unavailable, report the concrete
capability blocker and use only the documented connector fallback.

`step_id` is optional. Its default is deterministic for the same tool/arguments,
so identical mutations replay their receipt. Use a new explicit step ID when the
same arguments represent a deliberate new action. Guided server next calls already
supply distinct IDs for new check/publish attempts. Validation errors return exact
field errors before a dispatched checkpoint is saved, allowing argument correction
without creating a false uncertain mutation. Lost sync responses are reconciled
from the pre-recorded operation ID just like publish/process operations.

This reduces reasoning needed for orchestration, not business-code reasoning.
Verify with scripted callers that only pass task_id, wrong/missing arguments,
repeated calls and lost responses. Passing those tests is workflow evidence, not
a benchmark showing every low-capability model writes correct Odoo logic.

A new step/request ID cannot bypass a prior uncertain mutation in the same task.
Read-only reconciliation remains available; interrupted reads do not block writes.
