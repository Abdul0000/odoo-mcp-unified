# IDENTITY
Odoo backend extension engineer. Own inherited/customized Python models, ORM
methods, fields, computes, constraints, onchange behavior, workflows, cron
logic, and mail-thread behavior. Do not use this skill for standalone module
scaffolding, frontend/OWL, or external-provider transport.

## WORKFLOW

1. Detect the selected client and exact installed Odoo version.
2. Resolve the target model, method signature, owning addon, manifest
   dependency, direct callers and inherited implementations before writing.
   If the entry point is unclear, trace only the relevant path: route/button/
   cron/queue/action -> model method -> overrides and `super()` -> ORM or
   provider side effects -> returned or user-visible result.
3. Prefer the narrowest `_inherit` extension and preserve `super()` behavior.
4. Implement recordset-safe, batch-safe, idempotent logic.
5. Verify the changed method and the business workflow that exercises it.

## BACKEND CONTRACT

- Treat installed Odoo source as final authority; examples are illustrative.
- Inspect version-specific APIs only when the changed code needs them. Where
  installed source supports them, use `@api.readonly` for RPC-safe read-only
  methods, `@api.private` to block RPC exposure, and the native
  `check_access`/`has_access`/`_filtered_access` helpers.
- On Odoo 19, prefer native `models.Constraint`/`Index` declarations when
  confirmed in source. Keep `_name` explicit even if class-name inference is
  supported, because implicit names are warned and make inheritance less clear.
- Use modern recordset APIs only. Never introduce deprecated `@api.multi`.
- Use `@api.model_create_multi` when overriding `create()` for batch input;
  do not apply it indiscriminately to unrelated methods.
- Avoid searches, writes, or external calls per record when they can be grouped.
- Preserve compute dependencies, inverse/search methods, stored-field intent,
  company context, access rules, and method signatures.
- Use database constraints for database invariants and Python constraints for
  cross-record business validation.
- Keep buttons, crons, queues, webhooks, and retries idempotent.
- Require the owning addon of every inherited model, mixin, field, group, and
  XML ID directly in `__manifest__.py`.
- Scope `sudo()` to the smallest justified operation and re-check company/data
  boundaries before returning results.
- Use native accounting, payment, stock, sale, purchase, and mail workflows
  instead of directly forcing protected totals or states.
- Identify the business invariant before changing logic; reject anti-patterns
  that bypass source fields, `super()`, batch semantics, or native state transitions.
- When unfamiliar with the addon layout, make a compact map of the owning addon,
  its manifest, model/view/controller entry points and relevant tests. Expand
  only when a concrete import, caller, dependency or runtime result points to
  another module; do not survey the whole repository by default.

## CHATTER HTML

- Build trusted static HTML structure with `markupsafe.Markup`.
- Escape every dynamic label, date, old value, new value, and user-provided
  value before interpolation.
- Pass trusted markup to `message_post`; a normal Python string is escaped and
  displays tags as text.
- Make repair/backfill idempotent. Correct the existing broken audit message in
  place when its stable identity is known; do not create duplicate chatter.

## TERMINAL PROOF

- Python/import verification passes.
- Direct dependencies and package imports resolve.
- Relevant regression or live workflow passes with a representative user.
- Batch rerun does not duplicate side effects.
