# AI agent SDLC

## Intent, design, and task proof

Plan mode is opt-in and starts only when the user explicitly requests it. In normal execution, the agent can inspect, plan internally, and proceed without asking to switch modes. Approvals required by high-risk task or release policy are separate from Plan mode.

Use the team's issue, reviewed design, or immutable repository links as the authoritative records. The issue identifies the problem, affected users, constraints, and measurable outcome. The design records the proposed change, risks, and decisions. One reviewed document can hold both.

When opening a durable MCP task, pass `sdlc.intent` and `sdlc.design` as HTTPS links and include the existing `accepted_plan`. Plan approval covers both links, the allowed paths, base commit, acceptance criteria, and required verification layers. Changing any of those inputs requires a new approval. On publish, the existing task status records the verified commit. List the task ID in the PR for reverse lookup. Existing tasks and routine calls that omit the new fields keep their existing behavior.

Use the PR template and [review policy](../.github/REVIEW.md). A reviewer verifies the linked records and acceptance evidence before approving the final head. Configure GitHub branch protection for `master` to require a PR, dismiss stale approvals, require the regression and review evidence checks, and disallow bypass for agent credentials. That repository setting is outside the MCP server.

## Agent evaluation

The live workflow runs on trusted `master` pushes, nightly, and on demand. Configure `OPENAI_API_KEY` and pin `ODOO_AGENT_EVAL_MODEL` in the GitHub `agent-evals` environment, restricted to `master`. It loads versioned prompts, selected skills, and current tool definitions, then scores the model's structured tool decisions on synthetic tasks. It discards the calls and reports pass rate and duration per case. A failure blocks approval of that release SHA when the release gate is active. This job evaluates a bounded set of decisions. The existing deterministic suite covers workflow execution and deployment separately.

Extend the fixtures when a model routing or policy regression is found. Use a mocked client for offline checks. Run `python -m tests.evals.live` with the same environment variables for a live run. Keep production data, credentials, and client repositories out of the eval fixtures.

## Oracle release control

Stage the reviewed controls on Oracle in a root-controlled checkout. Run `sudo bash /path/to/reviewed-release/deploy/install-sdlc-controls.sh` before publishing the first gated release to `master`. The installer sets up the root-owned launcher and readiness monitor while the current release is serving. The authenticated incident endpoint becomes available when the new app release is live.

The GitHub webhook may queue a candidate, but it cannot approve one. An operator authorizes the exact merged commit after independent collaborator review of the final PR head, authoritative intent and design links in the PR, and successful regression and live eval runs for the same commit. To authorize and launch that candidate, substitute its full SHA and PR number:

```sh
sudo python3 /usr/local/libexec/odoo-mcp/release_gate.py approve Abdul0000/odoo-mcp-unified master FULL_SHA --pull-request PR_NUMBER
sudo systemctl start odoo-mcp-deploy@FULL_SHA.service
```

For a private repository, supply a read-only GitHub credential to the approval command via the operator environment. The root-owned approval records bind the repository, branch, release SHA, approver, reviewers, and CI run IDs. Reauthorization is needed if the branch advances. Direct pushes cannot produce qualifying PR evidence. Existing suite, readiness, transport, and rollback checks run during deployment. Historical approval is recorded at authorization time and should be removed by a root operator if revoked before rollout.

## Maintain and feed incidents back into planning

The Oracle timer checks local readiness once a minute. Three consecutive failures create one incident in a bounded queue, and a healthy sample marks recovery. Both events are logged in the systemd journal. The queue is available as `odoo-mcp://incidents/readiness` or at authenticated `GET /mcp/incidents` and locally in `readiness-incidents.json` under the connector state directory when the API is unavailable. If the connector state directory differs from `/var/lib/odoo-mcp`, set `ODOO_AGENT_STATE_DIR` in `/etc/odoo-mcp/readiness-monitor.env` to match. The monitor uses only local readiness and needs no application credentials.

During triage, record the incident's diagnosis and intended outcome on the authoritative issue. Link the incident ID as `sdlc.incident_id` on the durable task, record remediation and add the relevant regression case. Use the incident time and later task/PR/deployment timestamps to measure time to triage, time to recover, and repeat failures. This monitor covers connector readiness; the same triage queue can be extended for other production signals.

## Agentic staging handoff

A durable `target=deploy` task with `sdlc` links uses the reviewed route. After the accepted plan is recorded, `task_run` opens an isolated task branch, follows the existing test and publish evidence, and opens a GitHub PR or GitLab merge request on the selected client's configured branch. The repository token needs write access for reviews. The coordinator records the review attempt before creation. After a restart or lost reply, `task_resume` reads provider state and reconciles the linked review without creating another one.

An independent reviewer approves and merges the pinned head. Call `task_resume` to read the result. The coordinator compares the target branch head, first parent, and complete merged tree with the verified workspace inside the `task_deploy` gate before running an upgrade. A different base or tree requires fresh verification and a follow-up task. The module upgrade and live assertions still run through the standard deployment tools. Odoo.sh may build immediately when the protected branch changes, so keep the repository's own merge checks enabled.

If the readiness monitor opens an incident, use its ID in the triaged follow-up task's `sdlc` links. Include the new regression check in that task's acceptance criteria. This keeps the remediation and its verification connected to the original signal.
