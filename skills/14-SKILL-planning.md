# Odoo Scope & Impact Checklist

Internal checklist, not a mandatory user-facing phase. Follow `MCP_SYSTEM_PROMPT.md` and the configured provider lane; examples never authorize a second pipeline.

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
