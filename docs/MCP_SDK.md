# MCP SDK integration

The connector uses official `mcp==2.3.0` for standard Streamable HTTP at `/mcp`.
The low-level SDK `Server` fits this application because tool schemas already
come from a shared Pydantic registry and results include a deliberate control
envelope. Rebuilding each service as a decorator would duplicate that registry.
FastAPI and Pydantic are upgraded alongside the SDK and checked by the complete
regression suite. Existing `httpx` integrations remain separate from the SDK's
`httpx2` transport dependency.

## Mapping

| Responsibility | Implementation |
| --- | --- |
| Negotiation, JSON-RPC validation, notifications and standard HTTP behavior | Official SDK |
| Tools and annotations | Existing registry, converted to SDK `Tool` objects |
| Tool inputs, dispatch and errors | Shared `call_tool_result`, existing Pydantic validation and Odoo services |
| Prompts | Existing client-check and workflow instruction files |
| Read-only resources | Two fixed instruction URIs; no arbitrary filesystem access |
| Authentication | Existing bearer/API-key/OAuth validation before entering the SDK |
| Coding, polling, checkpoints, Git proof and deployment | Existing task and execution services |
| Legacy headerless requests and JSON batches | Existing compatibility bridge |

Course concepts follow the [MCP introduction](https://academy.claude.com/courses/introduction-to-model-context-protocol).
The implementation follows the supported SDK API rather than copying the
course's v1 examples: [low-level Server](https://py.sdk.modelcontextprotocol.io/advanced/low-level-server/)
and [ASGI integration](https://py.sdk.modelcontextprotocol.io/run/asgi/).

## Transport and lifecycle

An HTTP request uses the SDK if it includes `Accept: text/event-stream`
(normally `application/json, text/event-stream`), `MCP-Protocol-Version`, or
`MCP-Session-Id`. DELETE also enters the SDK. Requests without those transport
headers retain the previous JSON-RPC behavior, including batch compatibility.
The selector never reads a request body before passing it to the SDK.

`/claude/mcp`, `/chatgpt/mcp`, `/grok/mcp` and the existing Qwen URL-token routes
use the same selector. Qwen tokens retain their existing validation path.
`profile=task`, `profile=execution`, minimal-profile aliases and `X-MCP-Profile`
remain request-scoped; discovery does not authorize hidden low-level tools.

The host lifespan creates and starts one SDK session manager and closes it at
shutdown. Each new host lifespan gets a fresh manager. Standard SDK streaming
supports cancellation in both automatic and legacy clients. Legacy transport
sessions are bounded to 256 with a five-minute idle timeout and bound to the
validated credential through an opaque hash. The same API key works via bearer
or API-key headers; no new scopes or user approvals are introduced.
`ODOO_AGENT_MCP_MAX_SESSIONS` and `ODOO_AGENT_MCP_SESSION_IDLE_SECONDS` tune these
limits. Saturation returns 503 rather than creating unbounded sessions.

Legacy session requests must reach the same API worker; the shipped deployment
runs one API worker. Configure session affinity before scaling that API behind
a load balancer. Modern requests carry their protocol context per message.
Durable operation IDs and receipts remain in the existing service stores and can
be reconciled after a new transport handshake. No server-initiated list-change
notifications are advertised. Long-running operations continue through tool/job
polling; cancelling an HTTP call does not imply rolling back a durable job.

Set `ODOO_AGENT_PUBLIC_BASE_URL` for the real deployment hostname. Localhost is
trusted by default. Extra hostnames and browser origins use JSON-array settings
`ODOO_AGENT_MCP_ALLOWED_HOSTS` and `ODOO_AGENT_MCP_ALLOWED_ORIGINS`; do not derive
trusted hostnames from untrusted forwarded headers. The SDK validates Host and
Origin independently of existing token authentication.

## Validation

`python -m pytest -o addopts='' tests/test_mcp_sdk.py` checks real official-client
HTTP connections in automatic and legacy modes, negotiation, all provider aliases,
simple authentication, structured error/continuation behavior, prompts/resources,
invalid requests, notifications, cancellation, transport validation and concurrent profiles.
The complete regression suite also covers the existing REST and compatibility
surfaces, task recovery and execution proof. Tests use disposable local state and
do not deploy or contact a live Odoo database.

For manual MCP Inspector testing, select Streamable HTTP, enter the `/mcp` URL
and configure `Authorization: Bearer <token>` (or `X-API-Key`). Test initialization,
the task-profile manifest, `connector_health`, prompt retrieval and resource reads.
Use an approved test client/repository for any mutation; Inspector checks supplement
the regression suite.

## Progress, caching and correlation

Active RPCs stream actual workflow phases, job transitions and durable task
checkpoints through the SDK's request-scoped progress API. Progress counts phase
events, not invented percentages; totals are omitted. A bounded queue coalesces
repeated phases and `ODOO_AGENT_MCP_PROGRESS_INTERVAL_MS` throttles delivery.
Notifications stop when the RPC ends, including when a detached job continues.
Existing task/job polling remains the recovery mechanism for background work.

Modern catalogs and instruction resources return private cache hints, configured
by `ODOO_AGENT_MCP_CACHE_TTL_SECONDS` (60 seconds by default; zero disables client
caching). Server manifest caches immediately invalidate on settings changes;
client caches may remain valid until their bounded TTL expires. Keep profiles
stable for a client connection; use different endpoint URLs such as
`/mcp?profile=task` for persistent caches. No subscription invalidation is promised.
Resource error codes follow the negotiated version; an empty resource-template
catalog is supported. Headerless compatibility advertises no list-change channel.

Secret-free JSON operation logs contain request/trace IDs, tool names, outcomes,
durations and related task/job/operation IDs. Arguments, file contents, credentials
and opaque clarification handles are excluded. Trace correlation persists with
job, task and workflow records. The SDK's OpenTelemetry span is used when an
application tracing provider exists; otherwise a local correlation ID is generated.
No external collector is required. `GET /mcp/metrics` uses existing token auth and
returns bounded process counters, cancellations, errors and recent p95 durations.
Counters reset on API restart; durable workflow phase/queue timings remain stored.

## Standard business clarification

Modern clients declaring form elicitation can receive a standard
`InputRequiredResult` for a genuine business `clarification_answer`. The SDK
client collects the answer and retries the original call with `inputResponses`
and its opaque `requestState`. Unsupported clients retain custom `input_request`
and `next_call`. Technical argument generation and approval/authorization gates
are not converted to forms and do not introduce new authentication requirements.

Clarification handles reference server-private records, expire after ten minutes,
and bind to the authenticated credential and original tool arguments. Only a
server-generated continuation can execute. Writes are durable before dispatch;
completed responses are saved for exact retry replay. A cancelled or crashed
dispatch requires existing task/job reconciliation and cannot be automatically
replayed. Admission is bounded to 256 records with bounded lock buckets; oversized
continuations use the existing custom flow. No session affinity is needed for
these records when API workers share the same supported durable filesystem.

## Deployment checks and scaling

`python -m pytest -o addopts='' tests/test_mcp_advanced.py` adds real HTTP progress,
standard clarification with an official client, replay/tamper/cancellation checks,
restart recovery, session saturation/expiry and deployment-check coverage.

`python -m deploy.mcp_transport_check --env-file /etc/odoo-mcp/odoo-mcp.env`
checks the configured public HTTPS endpoint with automatic and legacy SDK
clients, reconnects, SSE, private cache hints, resources and API-key auth. It makes
no mutations. Configure `ODOO_AGENT_PUBLIC_BASE_URL` (or the existing OAuth issuer)
to the real proxy hostname. The deployment script checks loopback and public HTTPS
after restart, before clearing its rollback trap. Secrets are read from the env
file and never printed or placed in command-line arguments.

The shipped deployment keeps one API worker. Multiple hosts additionally require
a supported shared state/queue/locking design and legacy session affinity; turning
on extra workers alone does not distribute durable jobs. Test those boundaries
before horizontal rollout. New sampling, roots, subscriptions and STDIO features
are not required for this remote Odoo connector.
