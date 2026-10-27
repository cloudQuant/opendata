# Agent instructions

Read `CODE_QUALITY.md` before changing this repository. Keep production settings,
data recovery jobs and user-owned working trees unchanged unless the task explicitly
authorizes their modification.

## Local test execution

- Review fixtures and called services before running an explicit list of test
  files or node IDs. A marker such as `not e2e` does not establish isolation.
- Before any application import, use `APP_ENV=testing`, `TESTING=true`, empty
  `REDIS_URL` and empty `SENTRY_DSN`. Do not load credentials from `.env` for tests.
  Prefer an environment allowlist over inheriting service credentials.
- Local directory-wide pytest and `make gate` require a reviewed isolated
  environment for every included integration test. The aggregate gate reaches
  `test-cov`; passing static components does not authorize that broader run.
  Otherwise run safe components separately and report the aggregate as not run.
- The default-engine guards in `tests/conftest.py` are a precaution, not a network
  or filesystem sandbox. Explicit engines, direct drivers, subprocesses and
  user-supplied connection tests still require review of their targets.
- Database acceptance may write only to a fresh, explicitly owned instance and
  randomly named disposable schemas. Verify the instance identity before use and
  record cleanup. Never use a default connection to diagnose or undo a test run.
- Keep failed and interrupted logs. Missing evidence, skipped integration tests
  and historical results must not be presented as current passing verification.
  Coverage thresholds do not authorize expanding a test selection or contacting
  external resources. Do not lower thresholds or add exclusions to obtain a pass.

## Parallel changes

Use the main checkout based on `dev` for daily development. Parallel agents may
share that checkout with exclusive file ownership; the integration owner
serializes shared contracts, migration numbering, quality manifests, Git writes
and PR merges. Create an additional worktree only when explicitly requested or
when a concrete isolation need justifies it. Deliver changes through PRs to `dev`.
In delegated work, the integration owner must approve the exact local test
command before execution; other agents may prepare tests and run explicit static
checks. Preserve unrelated changes and frozen evidence archives.

## Review and merge scope

PR authors may self-review any PR. Record the decision and acceptance evidence
against the exact current head SHA, using a COMMENT review when GitHub does not
allow a native self-approval. A new head requires a new review. Do not represent a
self-review as an independent approval.

Only merge code whose stated acceptance scope has passed. Keep unaccepted or
failed work recoverable outside `dev`. A maintainer decision to defer CI repairs
does not turn failed checks, skipped tests or production acceptance into passes.
