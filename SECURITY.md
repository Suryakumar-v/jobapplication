# Security

## Model

Single-user, local tool. The API and n8n bind to `127.0.0.1`. Nothing is exposed to a network by default.

## Controls in place (Phase 1)

- **Submission safety**: `MANUAL_APPROVAL_REQUIRED=true` and `AUTOMATIC_SUBMISSION_ENABLED=false` are enforced at API startup and by the master n8n workflow.
- **API authentication**: when `API_KEY` is set, protected endpoints require the `X-API-Key` header (constant-time comparison). Binding to a non-loopback address without `API_KEY` is refused.
- **Secrets**: only in `.env` (git-ignored) or n8n credentials. `docker-compose.yml`, workflow JSON and code contain none; the workflow validator scans for hardcoded secrets and non-example email addresses.
- **Redaction**: log records and structured fields pass through a redacting filter (passwords, tokens, API keys, cookies, bearer values, emails, phone numbers, long opaque strings). URLs are logged without credentials, query or fragment.
- **Approval storage**: only SHA-256 hashes of approval tokens are stored. An approval is single use (consumed atomically before the browser is touched), expires after `APPROVAL_TOKEN_EXPIRY` seconds, is bound to one application, and is voided by a newer request, by cancelling, or after 3 wrong texts. The required text is exactly `SUBMIT <application_id>`.
- **Submission path (Phase 7)**: the only code that lifts the browser's submit guard runs after a valid approval and allows one click and one network request. n8n workflows cannot call the approval endpoints (enforced by the validator and tests), and `python run.py approve` needs an interactive terminal. Anyone holding `API_KEY` can still call the endpoints directly.
- **Exports (Phase 8)**: tracker cells are defused against spreadsheet formula injection (a leading `'` on `=`, `+`, `-`, `@`, tab and CR), stripped of control characters and length-capped, because job text is untrusted. Files are replaced atomically under `EXPORT_DIRECTORY` with fixed names, the endpoint needs the API key and returns no row data, and no tokens, answers or screenshots are exported.
- **Optional AI (Phase 9)**: off by default. Output is advisory: it never changes a score, recommendation, status, resume or form answer, and nothing in the approval or submission path uses it. The provider receives only the posting. Its reply is untrusted: unknown keys are dropped, text is stripped of control characters, links and e-mail addresses, lists are capped, and skills are compared with your profile locally by exact name (anything the profile does not list is reported as a gap, never added). Job text is passed as delimited data with an instruction to ignore embedded instructions; that reduces but cannot eliminate prompt injection, which is why the output has no effect on any decision. Misconfiguration (AI on without a provider, missing model, non-local endpoint without `AI_ALLOW_REMOTE`, non-https remote endpoint, credentials in the URL) stops the service at startup. Redirects are not followed, the response size is capped, error messages never include the prompt or response body, and `AI_API_KEY` is excluded from the settings repr.
- **Data integrity**: SQLite foreign keys enforced; one application per job; final statuses cannot change; naive datetimes rejected.
- **Container**: non-root user, published on loopback only.

## Not done / known gaps

- No CAPTCHA/MFA/bot-detection bypass will ever be implemented.
- No authentication on `GET /health` (it exposes only status and safety flags, no secrets).
- Docker files are unbuilt in the development environment.
- Dependencies are lower-bounded, not pinned; review before use on a sensitive machine.

## Reporting

Personal project: fix issues locally and rotate `API_KEY` / `N8N_ENCRYPTION_KEY` if a secret is ever exposed. Rotating `N8N_ENCRYPTION_KEY` invalidates stored n8n credentials.
