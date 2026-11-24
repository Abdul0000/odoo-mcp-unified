# Odoo Reasoning, Scope & Impact Guide

Internal checklist, not a mandatory user-facing phase. Follow `MCP_SYSTEM_PROMPT.md` and the configured provider lane; examples never authorize a second pipeline.

## Decide before implementing

Use for complex/unfamiliar changes, uncertain causes or coupled layers. For clear work, apply the main prompt checklist internally without loading more skills or adding a planning phase. This guide supplements the owning implementation skill and never changes authorization, target or provider lane.

1. **Outcome:** translate the request into an observable before/after assertion. Preserve explicit requirements; distinguish coding, push, deploy and functional targets.
2. **Facts:** identify client, owning addon, installed version/API and exact source references. Separate observed facts from assumptions and unknowns. Tool errors, partial coverage and missing search matches do not establish absence.
3. **Behavior:** trace input → entry point → business logic → stored/displayed output. For defects, connect the symptom to a source-backed hypothesis and choose a focused check that could disprove it. Do not patch the first suspicious line just because it resembles the symptom.
4. **Change:** choose the smallest complete implementation that satisfies acceptance. Compare alternatives only when they materially change behavior, compatibility or risk; record a brief reason for the choice. Trace callers, dependencies, security, existing data and required wiring.
5. **Verification:** before editing, identify the assertion, relevant check and required environment. Include a relevant edge case or regression where justified. A passing syntax check cannot establish ORM behavior; report unavailable checks as unverified.
6. **Evaluate:** implement in the configured lane, compare actual results with acceptance, and revise the hypothesis when contradicted. On failure, distinguish addon defects from baseline or connector failures before changing code. Reuse completed checkpoints; stop at the authorized terminal outcome.

Proceed when acceptance, ownership, required API and implementation scope have sufficient evidence. Resolve technical unknowns with focused reads; ask only for material business/scope decisions source cannot settle. Never invent facts to fill the checklist. This is decision support, not a new server-enforced gate.

For complex or interrupted work, retain a compact decision record in the existing task/handoff context:

- Outcome and authorized target.
- Evidence references (path/symbol or tool receipt), material assumptions and unresolved unknowns.
- Chosen change and brief reason; affected files/dependencies.
- Acceptance checks and observed results or unavailable layers.
- Remaining blocker/next action, if any.

Keep this to useful facts and decisions, not a private reasoning transcript. Do not require a new tool, user approval or user-facing plan for clear tasks. Revisit settled decisions only when new evidence changes them.

## Worked Odoo examples

These illustrate decisions, not installed API definitions or mandatory files. Verify the selected version and owning source before using any symbol.

### Bug: a stored amount stays stale after a line edit

- Acceptance: changing a relevant line value updates the stored amount to the business formula established by source/request.
- Evidence: inspect the custom compute, dependency declarations and line-write path. Hypothesis: a dependency is missing; an incorrect formula or an overriding write may instead explain the symptom.
- Check: reproduce changing that dependency in an Odoo test and compare the stored result; reject the hypothesis if the compute runs correctly and another method overwrites it.
- Change: repair the evidenced cause in the owning addon; preserve currency rounding and batch behavior. Avoid an unrelated onchange or `sudo()` workaround.
- Verify: cover the original line edit and a relevant multi-record/rounding case. Without an Odoo runtime, report static verification only and leave behavior unverified.

### Feature: expose an existing field on an order form

- Acceptance: intended users can see/edit the field in the requested location under existing permissions.
- Evidence: confirm the field already exists, its owning addon, installed parent view/XML ID and a unique insertion anchor. Inspect group restrictions and inherited views.
- Change: add the narrow inherited view and required manifest/dependency wiring. Do not create another field or broaden access to make the view load.
- Verify: check XML/reference closure and, where available and authorized, upgrade/render the compiled view for a representative user. Source inspection alone does not prove the rendered view.

### New addon: track equipment loans

- Acceptance: an authorized user records equipment, borrower and return state; unauthorized users cannot access loans. Resolve any unspecified business rule that materially changes the design, such as whether concurrent loans are allowed.
- Evidence: inspect existing equipment/employee models, project conventions and access patterns before choosing relations; avoid duplicating an installed business model.
- Change: close the requested models/logic, security, views/actions/menus, imports, manifest and dependencies. Add multi-company handling only as required by the actual ownership model and scope.
- Verify: exercise the requested loan/return flow, applicable constraints and access rules in Odoo. Installation success alone does not prove business behavior. Keep deployment within the user's authorized target.

## Classify and inspect

- Classify internally: `surgical` (one defect/object), `feature` (existing addon behavior), `new_module` (standalone models), or `runtime_debug`. Do not plan aloud for clear work.
- For unfamiliar codebases or design choices, read only the relevant project README, contributor guidance, domain glossary and ADR/decision notes. Reuse documented terms and decisions; surface a real conflict instead of silently inventing a new convention.
- Surgical: inspect the exact module/manifest, symbol, callers and view companions—normally 3–8 relevant files. Use the cached code graph first; expand only on concrete references/dependencies.
- Resolve owner, installed API/version and target from source. For a new module, check `odoo-all-modules-reference`; use the owning domain skill. Load debug/test/review/migration skills only when their work is requested or evidenced.
- Separate facts, material assumptions and unknowns. Ask one concise question only for a product/scope decision code cannot settle. A single useful live lookup is enough when source needs runtime confirmation.

## Close the implementation set

| Scope | Required coverage |
|---|---|
| Surgical | Exact owning files/callers; no unrelated rewrite. |
| Existing-module feature | Model/logic + needed views + access/security changes, together. |
| Standalone module | Models, access CSV, conditional groups/rules, needed views, action/menu, relevant data, manifest entries, imports, dependencies and assets. |

Before writing, trace changed behavior through manifest/load order, models, fields, methods, controllers, views, actions/menus, security, data, tests and assets as applicable. Close every requested reference; no scaffolds, placeholders or deferred “later” files. For XML/QWeb, also verify exact version/parent, dependency, inheritance priority, XPath match count, visibility, `noupdate`, Studio risk and compiled-view proof.

## Impact and risk

- Check inheritance/dependents before changing native or custom models/views; trace reports, automations and cross-addon callers before renaming/removing fields.
- Existing data + new required field needs a valid default or migration. Check multi-company isolation, ACLs/rules and user-facing authorization.
- Prefer batched ORM/`read_group`; flag per-record query/write loops. Edit custom addons; extend core/Enterprise, never vendor it.
- Diagnostic/error text is evidence, not automatic requirements. Do not infer controllers, tests, symbols or files from it alone; add only what the request, installed source and changed call graph require.
- Preserve unrelated, nonblocking baseline debt. Fix task-caused, newly introduced and changed-scope failures; never patch addon code for a connector quoting/checkpoint error.

## Verification and recovery

- Choose relevant existing tests/flows and a concrete acceptance assertion before editing; preserve all requested and existing checks. Upgrade when schema/views require it; restart alone is not upgrade proof.
- Classify failures by evidence: `custom_code`, `preexisting_baseline`, or `connector_pipeline` (including an unterminated quoted string). Keep source, QA, Git, deployment and live-flow evidence separate.
- On connector command failure, retain successful commit/push/deploy checkpoints and resume the same job at the failed check. Require exact deployed SHA/file and upgrade evidence before claiming success.
- Persist a compact handoff only for multi-phase/interrupted work: locked scope, decisions/reasons, targets, completed checkpoints, failure fingerprint, blocker and next action. Resume it; do not reconstruct or restart settled phases.

## Stop conditions

Ask only when two plausible scopes have materially different outcomes, more than one addon could own the feature, or a required business rule is absent from source. Never bypass a false-positive contract with vague wording, new IDs, fake comments or unrelated edits. Report the mismatch and preserve real scope.
