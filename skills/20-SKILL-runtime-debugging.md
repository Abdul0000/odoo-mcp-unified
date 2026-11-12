# IDENTITY
Odoo runtime debugging owner. Use only for an actual traceback, repeated
failure, unclear runtime behavior, controller/cron/queue/API failure,
browser-console defect, access error, install/upgrade error, or measured
performance defect. A clear deterministic edit remains a normal code-change
task and must not load this skill.

## DEBUG LOOP

1. Reproduce or capture the exact symptom with a stable correlation/session ID.
2. Read the server-filtered error pack: at most 150 recent source lines by
   default, ERROR/CRITICAL plus complete traceback context; ignore unrelated
   INFO/WARNING noise.
3. Orient on the earliest evidence-backed causal failure, not its downstream
   symptoms; separate verified facts, assumptions, and unknowns.
4. Map trusted evidence to the failure origin: `custom_code`,
   `preexisting_baseline`, `connector_pipeline`, `provider_transport`,
   `missing_context`, or `context_degradation`. Only `custom_code` evidence
   authorizes a module repair; each other origin redirects to its own recovery
   path.
5. Form one falsifiable hypothesis and apply the smallest complete repair
   through `workflow.odoo_debug_and_fix`.
6. Reuse the existing code-change, QA, safe-deploy, resume, and replay flow.
7. Replay the exact failure; refine the hypothesis only when evidence falsifies
   it, and expand evidence only if the same diagnosed failure persists.

## ON-DEMAND TRACE BUDGET

- Trace only when the failing entry point or side effect is unclear.
- Follow the exact controller/button/cron/queue entry, relevant overrides and
  `super()`, writes, external calls, and return/side effects.
- Use file/line evidence and mark uncertain branches; do not force diagrams,
  enumerate unrelated paths, or claim query counts without instrumentation.

## DEBUG CONTRACT

- Preserve the full raw output as an artifact; keep the primary response small.
- Redact secrets and sensitive payload values.
- Never patch module code to repair connector quoting, SSH, transport, or job
  orchestration failures.
- Do not retry blindly or restart completed commit/push/deploy checkpoints.
- Keep per-record batch failures isolated.
- Manual text and booleans cannot prove a fix.
- A repeated-failure override requires a structured diagnosis whose evidence
  IDs exist in the same bound server-owned debug session; narrative text or
  evidence from another task cannot unlock it.
- Claim `verified_fixed` only when deployment proof and immutable replay/live
  assertion both pass; otherwise report `implemented_unverified` or the exact
  blocker with evidence.
