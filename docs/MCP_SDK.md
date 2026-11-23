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
