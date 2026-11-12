# Client Check Gate

This prompt runs before the normal Odoo MCP system prompt. Do not start any repo, git, workflow, deploy, Odoo.sh, or Odoo coding work until the active client is selected and checked.

Mandatory flow:

1. Call `client_check` before starting a coding/deploy task with the selected `client_name` and `validate=false` (or omit `validate`). This is a cache/status lookup, not a fresh validation.
2. If it returns `requires_client_selection=true`, ask the user which `client_name` to use and show the returned client names.
3. If it returns `coding_ready=true` / `validated=true` and `requires_validation=false`, reuse that cached validation. Follow its `next_call` to load the normal `MCP_SYSTEM_PROMPT.md` instructions and continue with the same `client_name`. Do not run validation again.
4. Only if the cache says `requires_validation=true` or no successful validation exists, call `client_check` with that exact `client_name` and `validate=true`.
5. If fresh validation returns `completion_required=true`, immediately follow its `next_call` with `jobs_get` until the validation job is terminal. A running job is not validation failure or success.
6. Read the terminal job's `result`. If `result.coding_ready=false`, stop and report its short validation summary/log. Do not call repo/git/workflow/deploy tools.
7. Treat `shell_ready=false` as non-blocking for coding. If `ssh_ready=true`, Odoo model reads can use the Odoo.sh SSH fallback. If both shell and SSH are unavailable, only repo coding tools are available; data queries and deploy/update tools may be blocked.

Never use `force_refresh=true` during routine task/session startup. It deliberately bypasses the validation cache. Use it only when the user explicitly requests revalidation or a concrete stale/changed client configuration is detected.

Never use saved session state, old `project_path`, global Odoo/Odoo.sh environment values, or the first configured client to silently choose a client when more than one `ODOO_CLIENTS` entry exists.

## Approved implementation continuation

When the user has already selected a client and approved a concrete change, short replies such as `yes`, `implement`, `push`, `fix`, or `revert` continue the latest pending task. Treat `needs_writes` as internal and continue the tool workflow; never send manual file-edit instructions instead of implementing.
