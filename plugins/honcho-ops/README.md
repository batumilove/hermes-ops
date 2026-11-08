# Honcho Ops

This external Hermes plugin owns the Honcho production health monitor and queue
diagnostics formerly maintained in the application fork. It registers two CLI
commands through `ctx.register_cli_command`; it adds no model tools, hooks,
workers, dependencies or changes to the conversation prompt.

```bash
hermes honcho-ops monitor
hermes honcho-ops queue
```

The monitor preserves the existing contract: healthy runs produce no stdout;
alerts are formatted for verbatim script-only cron delivery. Its state remains
`cache/honcho_monitor.json` under the owning profile, and local ingestion reads
that profile's `state.db` (or the exact configured database-file override).
Transcript ordering does not prove a completed,
write-eligible human turn: unbound, truncated or unavailable local activity stays
UNKNOWN and produces a verification advisory rather than an ingestion outage.
An empty, complete activity window is idle; global remote inventory does not
prove per-session ingestion. Independent API, database, disk and chat failures
remain alerts. Existing `HONCHO_MONITOR_*` and `HONCHO_DIAGNOSTIC_*`
overrides retain their meanings. Probes use the existing SSH/tbot transport and
bounded read-only database queries. A monitor run can issue the configured chat
health probe; it is not an offline validation command. Queue diagnostics read
work units and logs without modifying containers or queue state.

The CLI registration requires in-process plugin loading. Hermes currently skips
`register_cli_command` for plugins configured with host isolation. Do not relax
an operator's isolation policy to make this plugin work; the migration's CLI
preflight refuses an unavailable command before changing the consumer.

## Installation and consumer migration

Use a reviewed immutable operations commit, with the owning installation's CLI
and intended profile selected explicitly:

```bash
hermes plugins install https://github.com/batumilove/hermes-ops/tree/REVIEWED_COMMIT/plugins/honcho-ops --ref REVIEWED_COMMIT --enable
hermes honcho-ops monitor --help
```

Replace both `REVIEWED_COMMIT` placeholders with the same full 40-character commit.
The explicit `--ref` keeps future plugin updates pinned; a commit in a tree URL
selects the initial source but does not itself pin the native installation record.

For an existing `scripts/honcho_monitor.py` cron consumer, record the wrapper's
owner and group, then run the migration as its owning cron user. The helper
preserves the mode, but does not preserve ownership. Run
`scripts/honcho/migrate_monitor_entrypoint.py` from the reviewed operations
checkout with `--hermes-home`, `--hermes-cli`, and the inspected wrapper's
`--expected-sha256`. It checks the installed command, saves the exact old bytes
beside the wrapper, then atomically replaces only that wrapper. The job's id,
schedule, delivery destination, enabled flag and no-agent setting are unchanged.
The new wrapper invokes the supplied installation CLI and inherits the cron
profile's environment. It imports no application-internal monitor module.

If filesystem protection prevents creating the backup or replacing the wrapper,
record the native protection flags of both the directory and the wrapper. Stop
and obtain scoped operator authorization before unlocking either path; unlocking
the directory does not clear an immutable flag on the wrapper. Verify the
replacement and backup owner/group against the recorded values and have the
authorized operator restore them if needed. Restore every removed protection to
its corresponding path immediately after the attempt, including on failure and
on the replacement wrapper after a successful atomic rename. Use a `finally`
block or shell trap so failure cannot skip restoration.

Verify the installed plugin revision, a fresh CLI invocation and the next
scheduled run before deleting the core modules. Rollback restores the saved
wrapper while those core modules remain installed; after core removal, install
the previous application revision with its included updater before restoring
the old wrapper. Never rewrite or restart the gateway as part of this migration.

`SOURCE.json` records the source revision and original paths. The migrated
behavior tests live in `tests/honcho` in this operations repository and run via
the pinned Hermes host's `scripts/run_tests.sh`. The plugin workflow exercises
Linux and macOS, installed CLI discovery, per-profile state, and wrapper recovery.

## Durable verification receipts

The compatible receipt-emitting Honcho writer records the admitted completed-turn
boundary before its asynchronous write is queued. Local receipts are private
SQLite files under the owning profile's `cache/honcho-receipts/`. They contain
session/workspace and part identities, timestamps and remote IDs; no text, prompt
hashes or credentials. Confirmed parts require the server's echoed correlation
metadata and matching workspace/session. The monitor independently checks those
exact remote identities against PostgreSQL. This proves the sampled admitted
writer boundary only; it is not transcript-wide coverage or proof of downstream
conclusion quality. Missing or truncated evidence remains unverified. Uncertain
requests are never automatically replayed by this tooling.

`scripts/honcho/nous_auth_proxy.py` preserves the deployed proxy's token and
1536-dimensional embedding behavior, adding private HTTP-attempt receipts with
requested model, sanitized upstream origin, caller, outcome and error class.
Neither request nor response content is recorded. Completed receipts retain seven
days; outstanding debt is preserved. A successful upstream response is not proof
of semantic output or complete coverage of every pipeline stage.

Fresh configuration inspection reconstructs route fields with each deployed
image's actual settings parser in an empty credential environment, network-blocked
and without file sources. Existing `.env` or TOML files make this check unverified.
Reports distinguish that reconstruction from the running process's memory and
from observed HTTP attempts. All five configured dialectic levels are displayed.

Deploy the proxy atomically through the existing scripts lifecycle, preserving
its exact prior bytes/mode and directory protections, then restart only its own
user service. Install both plugins at immutable refs through `hermes plugins
install --force --ref ... --enable --yes-deps`; activate the writer with the
supported gateway lifecycle command. Preserve receipt databases during code-only
rollback. Verify a naturally admitted turn; never send synthetic conversation
content to manufacture acceptance. The monitor's regular cron schedule and
delivery destination remain unchanged.


The proxy defaults to loopback binding and loopback callers. A remote Honcho
installation must explicitly set `NOUS_PROXY_BIND` and `NOUS_PROXY_ALLOWLIST`
(IP addresses/CIDRs); rejected sockets do not load the provider credential.
Authenticated redirects are limited to the upstream origin. POST bodies have a
size cap and absolute read deadline; streaming responses preserve SSE content type
and flush incrementally. Receipt IDs reset per request on persistent connections.

Set `NOUS_PROXY_RECEIPT_PATH` to the private authoritative SQLite file. When the
monitor uses a different profile home, explicitly select that same accessible file
with `HONCHO_MONITOR_PROXY_RECEIPTS`; `HONCHO_MONITOR_PROXY_CALLER` optionally
restricts verification to the Honcho host's observed source IP. A remote store must
be made accessible through an authorized collector/mount; this plugin never guesses
another profile's store or treats an unavailable file as successful verification.
Unfinished attempts remain debt outside the recent activity window. Part reads,
route reads and completed-receipt retention batches are bounded; receipt indexes
keep admissions from repeatedly scanning the full history.

### Exact stage attribution

A compatible Honcho server emits an opaque `X-Honcho-Request-ID` and closed
`X-Honcho-Stage`/`X-Honcho-Call-Purpose` headers at the actual SDK send boundary.
The proxy accepts only the known stage/purpose taxonomy and consistent operation
(embedding versus chat), and stores these fields in a separate correlation table.
The original routes schema remains compatible with older readers/writers.
Application request IDs survive SDK retries; each HTTP attempt retains its own
unique receipt ID and outcome. No stage is guessed from a model name or timestamp.
The monitor reports sampled stage outcomes and `not_observed` for absent stages;
untagged legacy calls stay explicitly unattributed. These are sampled execution
receipts, not a claim of transcript-wide completeness or output semantics.

The server can also persist private caller receipts before provider dispatch,
using `HONCHO_REQUEST_RECEIPT_PATH`. On the Honcho host, persist the API and
deriver stores separately as `/var/lib/honcho-receipts/api.sqlite3` and
`deriver.sqlite3`, mounted privately into their respective containers. The SSH
collector reads at most 1000 recent or unfinished requests per store, including
old unfinished debt. Caller errors and cancellations alert even if the proxy is
unreachable. Opaque request IDs join caller and proxy attempts; an unmatched
success remains explicitly unmatched rather than being classified as an outage.
A missing store or truncated window cannot establish idle or complete coverage.
