# v3.3 real-coder tool upgrades

- feat: `ws_edit` self-heals a search that differs only by per-line indentation/trailing whitespace when it matches exactly one place; the replacement is re-indented to the file's own indentation. Result lists `healed`. Ambiguous or content-different searches still fail with `closest`. Healed edits replay correctly after restart/rebase.
- feat: `ws_commit` `message` is optional; a conventional `[IMP] module: update files` message is generated, so the agent never stalls composing one.
- tests: `tests/test_real_coder_tools.py` (13 cases). Full regression: 745 passed, 2 skipped.

# v3.2 real-coder prompt

- prompt: REAL CODER MODE moved first and hardened: tool call first, no permission questions, parallel reads, one batched `ws_edit`, `ws_symbol` verification, all `findings` fixed before `ws_commit`, 4-line final report. Prompt stays under 12 KB.

# v3.1 fixes (test-suite green)

- fix: output-schema scanner inspected the thin `_dispatch` wrapper (added by the no-checkout redirect) instead of `_dispatch_inner`, so every tool lost its pydantic `outputSchema`. It now scans `_dispatch_inner`.
- prompt: tightened `MCP_SYSTEM_PROMPT.md` (13.3 KB -> under 12 KB) without removing any contract marker, so REAL CODER MODE fits the size budgets again.
- result: 731 passed, 2 skipped (full regression, `-o addopts=""`).

# Keep the coding agent moving (no-checkout redirect)

- fix: `workflow_odoo_code_change` on a host with no local checkout (e.g. Render) no longer fails with "Project path not found"; it returns `status: use_ws_lane` with `next_call: ws.open` and the task carried over, so the agent continues in the same turn (`services/checkout_redirect_service.py`).
- general: `_dispatch` turns a missing-checkout 404 from `workflow.odoo_code_change`, `module_analyze`, `odoo_debug_and_fix` and `frontend_visual_implement` into the same redirect for any client that has a repo configured (an explicit `project_path` keeps its error).
- prompt: new REAL CODER MODE (act first, no plan block, resolve unknowns from source, 'push/do it/go' runs the whole chain, never stop on recoverable failures, short final report); results carrying `next_call` run in the same turn; code/change/fix routes to the workspace lane first and is provider-neutral (GitHub or GitLab).

# Review fixes (post v3)

- tests: fake clocks scoped per module (`patch_module_clock`, `advance_clock_on_job_wait`); the old patches mutated the global `time` module and hung the suite. Flaky timing test uses CPU time, best of 3.
- tests: stale tool-set expectations include ws_* (`WS_TOOLS`); size budgets re-measured (tools/list 80 KB, instructions 15 KB).
- deps: `requirements-dev.txt` (jsonschema, pytest-asyncio); `pytest.ini` asyncio strict mode.
- security: OAuth signing never uses a public fallback key (derived HMAC from API token, or random per-process); connector token compared in constant time; hosted-deployment guard covers Render, Koyeb, Fly, Heroku, Cloud Run. NOTE: OAuth access tokens issued before this change stop validating once; clients re-authorise.
- bug: `CommandResult` was undefined on the job-resume fallback path in routers/tools.py.
- arch: ws.* dispatch is one handler table (`_WS_HANDLERS`); input models derived from it.
- hygiene: FastAPI `lifespan` replaces `on_event`; `ruff.toml` (real-bug rules) and a CI workflow with a 10-minute timeout.

# odoo-mcp ws upgrade (v3)

v3: visible WIP branch (real files), ws_status, commit links + default-branch note, edit results say 'published: false'.

New tools: ws_open, ws_read, ws_grep, ws_edit, ws_diff, ws_lint, ws_commit, ws_discard,
           ws_outline, ws_scaffold, ws_extend, ws_symbol, ws_status
New services: toon_service, lint_service, github_commit_service, workspace_service, workspace_factory,
              outline_service, scaffold_service, symbol_service, job_retention
Fixes: delete->unwire+lint, binary/size guards, assets warning, lazy open, journal-only checkpoints (async),
       blob dedupe, keep-alive transport, hot-path commit, job prune/wait, tools/list cache, blobless clones
Tests: tests/test_toon_service.py test_workspace_service.py test_scaffold_outline.py test_job_retention.py
       test_ws_tool_wiring.py test_evals.py (+ tests/evals, tests/support/fake_github.py)
Run:   python -m unittest tests.test_toon_service tests.test_workspace_service tests.test_scaffold_outline \
                          tests.test_job_retention tests.test_ws_tool_wiring tests.test_evals
