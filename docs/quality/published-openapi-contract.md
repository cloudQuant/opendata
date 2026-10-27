# Published OpenAPI contract gate

The OpenAPI compatibility check runs from the actual `opendata.main.app.openapi()` document. It runs with `APP_ENV=testing` and `TESTING=true`, disables Redis, does not enter FastAPI lifespan, and points both database hosts at a non-routable test name. `TESTING=true` suppresses import-time logging integrations. It performs no database, source-provider, or network request.

## Frozen surfaces

- [`afi204-v1-openapi.json`](../afi204-v1-openapi.json) freezes the 140 legacy HTTP paths mounted under both `/api/v1` and the compatibility `/api` prefix (166 operations). It contains the referenced schemas and security scheme only.
- [`afi204-v2-openapi.json`](../afi204-v2-openapi.json) freezes the six published-data v2 paths and their referenced schemas.

The v1 document was captured from the live app on 2026-10-26. The v1 artifact SHA-256 remains `2b738ff5ab2d2aa8a2603c908a05256a1b8b7522f72aa5a699ba963d4271bf74`. The v2 surface was recaptured and independently reviewed on 2026-10-27 for W11; its artifact SHA-256 is `b5ef23a6d2c0910a900d886832ee2650d1cbbd3baf98816551eb07a655e612aa`. The checker pins both hashes in `scripts/quality/check_published_openapi.py`. There is no `--update` or self-comparison path.

The W11 v2 update adds optional `instrument_id` and `capture_id` query/body fields and the optional `selection` response field with its strict pair schema. It preserves existing routes, authentication and required fields. Runtime validation requires the pair for a new instrument/capture-profile selection and checks repeated selectors against an existing snapshot; legacy datasets reject these selectors. The artifact retains its existing v2 pre-release `info` annotation; paths, components and all other top-level fields come from the actual app capture. This update does not establish source permission, data completeness or production activation.

## Compatibility rules

The gate permits a new path or method, and additional optional properties in existing response objects. It blocks removal of a frozen path, method, response status, response media type, or existing response field; a new required response field; changed response type, enum, nullability, requiredness, or other schema constraints; changed auth requirements or security schemes; and changed operation IDs. Existing parameters and request bodies are held to an exact contract, so even a safe widening requires review before it can pass.

Inline schemas and local `#/components/...` references are walked recursively. The checker follows references through arrays and `allOf` / `anyOf` / `oneOf`, and terminates legal recursive schemas using a visited-reference set. External, missing, malformed, or changed references fail closed. Path Item `$ref` is currently unsupported and blocks until the checker gains operation-aware comparison for that shape. Unrecognized schema keywords and changed composition structures fail closed instead of being skipped.

Operation summaries, operation descriptions, tags, response descriptions, content descriptions, and schema title/description/example annotations may change without a compatibility failure. Parameters, request bodies, path-item metadata, security requirements, and non-documentation operation fields are held to an exact contract, so even descriptive metadata in those locations requires review. New response status codes, media types, or parameters are conservatively blocked until their compatibility semantics are explicitly reviewed and the rule is changed with tests. Top-level contract fields, including `servers`, `security`, `jsonSchemaDialect`, `webhooks`, and extensions, are preserved and compared; a missing field differs from an explicit `null`, and invalid field shapes fail closed.

## Baseline update review

When an API change is intentional, first review the live OpenAPI diff and compatibility impact. A reviewer must verify the new behavior, update the relevant frozen artifact from the actual app document, and update that artifact's SHA-256 pin in the checker in the same reviewed change. Do not make a baseline update merely to silence the gate. The checker itself never writes either baseline.

Run the focused gate and its behavior tests with:

```bash
make published-openapi-check
APP_ENV=testing TESTING=true REDIS_URL= python -m pytest --no-cov tests/test_published_openapi_gate.py
```

CI invokes `make gate`; the aggregate gate invokes `published-openapi-check` as a named blocking sub-step.
