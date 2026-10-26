# AFI-204 reviewed command entry

This directory documents the install-time shape for the reviewed CLI. It does
not install or enable a service. The production composition root still needs
to construct the reviewed task catalog, database engine, dispatcher, worker
SPI, and role plan before this command can run.

`opendata.reviewed_cli.run_reviewed_cli()` accepts only `--request PATH` and
JSON schema version 1. Use `-` to read stdin. The parser bounds input to 64 KiB,
depth 12, and 8,192 JSON nodes; rejects duplicate keys, unknown operation
fields, symbolic-link files, execution and connection selector keys, URI
userinfo, recognized credential query parameters, and common DSN credential
assignments. It never imports application paths from the request, loads
dotenv, reads `settings.py`, opens a database, or starts a scheduler by itself.

The trusted launcher must bind the reviewed application statically in code,
then call the library entry point. For example, after the deployment has added
its own `reviewed_application.py` composition root:

```python
from reviewed_application import reviewed_cli_application

from opendata.reviewed_cli import run_reviewed_cli

if __name__ == "__main__":
    raise SystemExit(
        run_reviewed_cli(["--request", "-"], application=reviewed_cli_application)
    )
```

Scheduled dry-run request:

```json
{"schema_version":1,"operation":"scheduled","job_id":"market-daily","version":"v1","occurrence":"2026-10-26T09:30:00Z","dry_run":true}
```

Worker-once request:

```json
{"schema_version":1,"operation":"worker_once","worker":"opendata-worker-01","lease_seconds":60}
```

Send a request with the launcher on stdin, for example:

```sh
printf '%s\n' '{"schema_version":1,"operation":"worker_once","worker":"opendata-worker-01","lease_seconds":60}' | /opt/opendata/bin/reviewed-entry
```

The CLI exits 0 for queued, running, dry-run, idle, or successful outcomes; 1
for an operation or run failure; 2 for malformed or rejected requests; 78
when a reviewed application/role is absent; and 130 for interruption or a
cancelled run. Receipts contain only the run key and bounded status metadata.
A worker interruption may leave a leased run in `running` until lease expiry;
this adapter does not implement hard cancellation or worker supervision.

The no-argument module entry point intentionally fails with exit 78. A future
deployment must add a statically reviewed launcher and systemd unit, declare
the sink IDs owned by that reviewed app, stop legacy writers for those sinks,
and then install and enable the unit through the host change procedure. Do
not run this alongside legacy collectors writing the same sink.
