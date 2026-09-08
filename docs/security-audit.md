# Security Audit Contract

The gateway is the audit authority. The GUI displays the existing SQLite
`security_events` store; it does not execute or independently infer operations.

## Categories

| View | Recording policy |
| --- | --- |
| File access and changes | Sensitive and external paths remain individual records. Successful ordinary workspace operations are summarized by project, session, turn, action and tool. |
| Commands and system | One operation per execution. Process polling updates the original command when its session identity matches. |
| Network and data | Web tools, model requests and MCP invocations. Successful searches and logical model requests are summarized per task. MCP invocation alone is not proof of external transmission. |
| Decisions and approvals | A filter over the original operations, including approvals, denials, timeouts and blocks. It does not create duplicate events. |
| Policy and administration | Policy changes include before/after values. Export records generation of the response, not confirmation of the GUI saving the file. Clear and its administration record commit together. |

## Lifecycle and Ownership

An operation is recorded before execution. Approval and completion update the
same row. `decision` is independent of `result`: an approved command can fail.
`details.lifecycle` preserves bounded transitions and timestamps. A pending
record means completion has not been recorded; it is not proof of continued
execution after a gateway crash.

Project/session/turn/tool-call identifiers and the researcher label are retained
when available. Aggregates clear the individual tool-call ID, retain task
ownership, and expose `operation_count`, up to 20 path samples and the last
completion timestamp. Failed or sensitive operations are never aggregated.
Commands report actual exit codes and distinguish a running process from a
completed one. Without a subsequent poll, a background command can retain its
last observed running state.

## Privacy and Coverage

Security audit strings and metadata are sanitized at the storage boundary and
again on reads for legacy records. Do not store tool argument values, document
bodies, model messages, reasoning, search text, command output or raw errors.
Commands retain a bounded, sanitized preview. Common credential formats,
authorization headers, JWTs, URL query values and recognized inline request
payloads are removed before truncation. Arbitrary opaque secrets embedded in
unstructured shell programs cannot be reliably recognized; prefer structured
metadata when adding audit support for a tool.

The instrumented Web tool HTTP clients record sanitized request URLs, methods
and received status codes, including redirects. Request bodies and headers are
excluded. Lists are bounded to 20 requests with a separate request count.
SDK-internal search requests, arbitrary subprocess traffic and MCP downstream
requests are not comprehensively observed. Unknown destinations remain unknown.
Model entries describe logical provider requests, including internal retries,
not individual HTTP attempts. This is an application audit, not packet capture.

Existing records are not rewritten to invent missing outcomes. Legacy MCP rows
are mapped into the network/data view; sensitive values are omitted from API
responses and exports. This does not erase historical bytes already on disk or
in files exported before the update.
