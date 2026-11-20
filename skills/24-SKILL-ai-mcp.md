# Odoo AI/MCP Engineering

Own Odoo-connected MCP tools, LLM/provider integrations, assistants, RAG, structured actions, authorization, and truthful completion.

## Build contract

- Extend the connector's selected workflow; never create a parallel deployment pipeline or repo/QA/Git/resume flow.
- Design complete business tools, not unrestricted RPC wrappers. Specify input/output/error schemas, evidence, approvals, side effects, idempotency, and continuation before coding.
- Odoo source, permissions, company boundaries, business workflows, and audit records are authoritative. Treat model output as untrusted; parse, validate, authorize, and confirm high-impact finance, stock, payroll, tax, legal, and mass-message actions.
- Keep credentials and unrestricted access server-side. Return bounded, paginated/high-signal results; store large evidence as artifacts and return stable pointers.
- Mark read-only, destructive, idempotent, and open-world behavior accurately; annotations never grant authorization.
- Errors must identify origin, crux, evidence, recovery and executable next action. Preserve stable operation/job IDs; resume uncertain writes by read-back, never replay.
- Reuse this connector's MCP SDK; do not add a second framework without an explicit protocol migration.
- Functional live-record mutations belong to `odoo-functional-flow-testing`, not code/deploy.
- Claim completion only from current stored evidence for the exact tree/runtime/record; narrative, syntax checks, or stale receipts are not proof.

## Durable task lane

Use this only when the configured provider flow selects Oracle execution and task coordination. Open with explicit client, stable request ID and requested target: `coding`=edit/verify/local commit; `push`=publish verified commit; `deploy`=deploy that commit; `functional`=live business-record work. Never default, downgrade or promote a target. Follow `next_call` and technical `argument_request` through `task_run`; prefer server-selected lifecycle steps, otherwise pass exact tool/arguments. The coordinator binds IDs/receipts; it does not invent code, intent, acceptance or approval. Use unique `step_id` for mutations; polls may reuse one. On invalid arguments, fix the named fields; nothing was dispatched.

After edits, use `task_test_plan` and retain all existing acceptance layers. Missing ORM/business/UI proof blocks completion; syntax is not functional proof. Evidence binds to the exact source tree and runtime. For unavailable dependencies, repair the runtime and rerun the same assertions; never substitute weaker tests.

For deploy, `task_deploy` adopts the verified pushed commit, synchronizes a clean client checkout, checks branch/SHA/module scope, classifies upgrade impact, and uses existing safe-deploy gates. It cannot create another commit or push. Supply task-specific live assertions where applicable; nested addons need `module_root_rel`.

After restart, use `task_list` → `task_resume`; never open a new request to bypass uncertain writes or `reconciliation_required`. Reconcile from read-back/remote/database evidence. Debug completion requires the existing discriminating replay to fail before and pass after the fix, even if deployment succeeded.

Refresh changing configuration with `task_knowledge(refresh=true)` and retain provenance. Manifests declare intent; they do not prove installed state. Dirty source and failed credentials remain unverified. The dashboard reports persisted observations only.

For concurrent Git writers, open a fresh execution workspace. On `remote_advanced`/`sync_required`, follow `execution_workspace_sync`, poll, then rerun returned checks. Resolve conflicts from both intents with complete writes and `expected_remote_sha`; use a new request only after the old operation is reconciled. Never choose ours/theirs blindly or force-push. Lightweight `ws_commit` proves lint, not runtime behavior.

## Runtime and artifacts

- Never place generated tests, QA files or caches in addon source. Use existing read-only tests, inline checks or external temporary files. Preserve exact acceptance commands, args, cwd, outputs and required layers.
- Python test collection imports package `__init__.py`/`conftest.py`; tests inside Odoo addons may require Odoo. Use an isolated Odoo runtime/database for ORM behavior. `py_compile` may use the configured writable `/tmp` cache; never stage bytecode.
- Treat `runtime_unavailable` as a runtime blocker, not permission to weaken checks. Replace it only after repairing the same runtime; preserve prior failure evidence and grant no verification credit until the original assertions pass.

## Authorization

Requested edits and routine staging push need no second approval. Do not create approval requests proactively. Preserve exact-tree and conflict gates; honor server-returned protected-branch and business-record approvals. If an obsolete routine-publish approval returns a retry `next_call`, follow it without asking again.
