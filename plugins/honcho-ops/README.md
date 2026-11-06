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
