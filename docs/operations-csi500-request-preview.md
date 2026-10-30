# CSI 500 request preview

Use the standalone command to inspect the fixed request shape for one caller-selected date:

```bash
python scripts/ops/preview_csi500_request.py --business-date 2026-10-30
```

The command prints `000905.SH`, the Fuyao HTTPS host and index-prices route, and a SHA-256 digest of the one-day query parameters built by the existing THS endpoint builder. It requires no sample target, database, calendar file, authorization, or API key. It does not read `.env` or process credentials, open SQLite, create gate state, enqueue a job, call a provider, or access the network.

The preview reuses the existing THS endpoints module, whose import chain includes the provider HTTP client. This project declares `httpx` in its `web` extra; install the web extra (`pip install -e '.[web]'`) or use an environment where `httpx` is already installed. Importing the client type does not create a client or send a request.

The supplied date is syntax-checked only. `9999-12-31` is rejected because the one-day request requires a representable next-day exclusive endpoint. The output explicitly reports `authorization_status=not_established`, `calendar_evidence_status=not_verified`, `target_status=not_required_not_accessed`, `gate_status=not_accessed`, and `execution_enabled=false`. A digest describes the caller-side parameters; it does not prove a trading session, source rights, sent bytes, provider response, or data availability.

For the golden-vector date above, the endpoint builder produces Shanghai-midnight milliseconds `start=1793289600000` and `end=1793375999999`. After the builder's values are converted to strings, the request-summary canonical JSON bytes are:

```text
{"parameters":{"end":"1793375999999","interval":"1d","start":"1793289600000","thscode":"000905.SH"},"version":1}
```

The SHA-256 digest is `f96956fd2392b15d771468793660c475059d6fb7b830103cc2d910b4b6ce75fd`. The preview command is a local planning aid and is not a collection permit or a live sample runner.
