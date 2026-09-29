# Project Status

Last updated: 2026-09-29 (Phase 10)

## Phase progress

| Phase | Scope | State |
|---|---|---|
| 1 | Foundation | **Complete and validated** (see below) |
| 2 | Job ingestion, normalization, fingerprint, duplicates | **Complete and validated** (see below) |
| 3 | Deterministic matching | **Complete and validated** (see below) |
| 4 | Resume tailoring | **Complete and validated** (see below) |
| 5 | Playwright service and mock application | **Complete and validated** (see below) |
| 6 | n8n workflows (full) | **Complete; all 11 files import into n8n 2.41.3 (CLI). Never executed** (see below) |
| 7 | Manual approval and submission gate | **Complete and validated against the synthetic site only** (see below) |
| 8 | Tracker export | **Complete and validated with synthetic data; opened once in desktop Excel 16.0 (see Phase 10)** (see below) |
| 9 | Optional AI | **Complete and validated with a mock provider and a fake HTTP transport; no real model was ever contacted** (see below) |
| 10 | Final end-to-end validation | **Complete for the local, synthetic-only path; Docker verified in GitHub Actions; n8n execution, real portals and real AI models remain untested** (see below) |

## Phase 1: implemented and tested

- Project skeleton, `requirements.txt`, `pyproject.toml` (ruff, mypy strict, pytest), `.env.example`, `.gitignore`, `.dockerignore`.
- `app/config.py`: environment settings with safe defaults; `safety_violations()` blocks startup if approval is disabled, auto-submit is enabled, or a non-loopback bind has no `API_KEY`.
- `app/utils`: redaction, rotating JSON logging (`application.log`, `browser.log`, `error.log`, `audit.log`), hashing, text and file helpers.
- `app/database`: SQLAlchemy 2 tables (jobs, duplicate_records, applications, status_history, tailored_resumes, approvals, approval_attempts, schema_version), schema versioning, repositories (application ID `APP-YYYYMMDD-NNNN`, status history, final-status protection, one application per job).
- `app/models`: candidate, job/search preferences, application status, question categories, scoring configuration (all validated from YAML).
- `app/main.py`, `app/api/health.py`, `app/dependencies.py`: FastAPI factory, `GET /health`, API-key dependency.
- `run.py` (Typer CLI) and `scripts/` (`initialize_database`, `validate_configuration`, `validate_n8n_workflows`, `install_playwright`).
- `config/`: candidate/search/safe-answer examples (synthetic, marked editable), sensitive-question keywords, scoring weights, portal settings (no portal validated).
- `n8n/workflows/`: master workflow with real startup validation (health + safety flags) and ten labelled placeholder workflows; `n8n/templates`, `n8n/credentials/README.md`.
- `Dockerfile`, `docker-compose.yml` (n8n + API, loopback only, no secrets in file).
- README, SECURITY.md, PRIVACY.md.

## Phase 1 validation results (2026-09-29)

| Check | Result |
|---|---|
| `python -m compileall app scripts tests run.py` | pass |
| `ruff check .` / `ruff format --check .` | pass / pass |
| `mypy` (strict, 40 files) | pass |
| `pytest` | **72 passed, 0 failed** |
| `scripts/validate_n8n_workflows.py` | 11/11 files OK |
| `scripts/initialize_database.py` | schema v1 created |
| `scripts/validate_configuration.py` | pass (3 example-data warnings expected) |
| `python run.py serve` + `GET /health` | `status: ok`, `startup_ok: true` |
| `AUTOMATIC_SUBMISSION_ENABLED=true python run.py serve` | refused, exit code 1 |

Environment used: Python 3.12.7 (installed per-user during this session), fastapi 0.141.1, starlette 1.7.0, SQLAlchemy 2.1.1, pydantic 2.13.5, playwright 1.63.0, typer 0.27.2, pytest 9.1.1, ruff 0.16.9, mypy 2.3.1.

## Phase 2: implemented and tested

- `app/services/job_normalizer.py`: URL canonicalisation (tracking params, `www.`, default ports, http/https, fragments, credentials removed; non-http(s) rejected), company/title/location/employment-type/workplace normalisation, HTML-stripped descriptions (50k cap), description hash (ignored below 50 characters), deterministic 32-hex fingerprint and `JOB-<12 hex>` job ID.
- `app/services/duplicate_detector.py`: checks in order fingerprint, URL, external ID (same company only), company + title (same or unknown location only), description hash, existing application, optional fuzzy description match (`FUZZY_DUPLICATE_THRESHOLD`, off by default). Each duplicate is stored in `duplicate_records` with its reason and the original job ID; no job, application, resume or browser work is created for it.
- `app/services/job_importer.py`: JSON (object, list or `{"jobs": [...]}`), CSV, pasted text (`Title:`/`Company:` headers), manual URL, webhook. Field-alias mapping, salary/date/workplace parsing, per-item results (`CREATED`, `DUPLICATE`, `REJECTED`, `NEEDS_EXTRACTION`, `DEFERRED`), `MAX_JOBS_PER_RUN` enforced, invalid items never stop the batch.
- `app/api/jobs.py` (all require `X-API-Key` when configured): `POST /jobs/import/{url,text,json,csv}`, `POST /jobs/webhook`, `POST /jobs/check` (validate, fingerprint and duplicate-check without storing), `GET /jobs`, `GET /jobs/{job_id}`.
- `python run.py import-jobs <file.csv|file.json>` for local files.

### Phase 2 validation results (2026-09-29)

| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | pass / pass |
| `mypy` (strict, 49 files) | pass |
| `pytest` | **159 passed, 0 failed** (87 new) |
| CLI smoke test (synthetic CSV, temporary DB) | run 1: 2 created, 1 duplicate, 1 rejected; run 2: 0 created, 3 duplicates |

Phase 2 limitations:

- A URL alone cannot be turned into a job yet: without company and title the item is reported `NEEDS_EXTRACTION` and not stored. Browser extraction (`POST /jobs/extract`) arrives in Phase 5.
- RSS feeds, public job APIs and browser extraction sources are not implemented (the `RSS`/`API`/`BROWSER` source values exist but nothing produces them).
- Description-hash matching is global, so an identical description under a different employer (for example a recruiter repost) is treated as a duplicate; review `duplicate_records` if a job seems to be missing.
- The n8n workflows `01`-`03` are still placeholders; wiring them to these endpoints is Phase 6. The endpoints were tested with the FastAPI test client and the CLI, not through n8n.
- Filters from `search_preferences.yaml` (excluded companies/titles, job age, daily application cap) are not applied yet (Phase 3).

## Phase 3: implemented and tested

- `app/services/matching_engine.py`: deterministic, no AI, no network. Weights come from `config/scoring_weights.yaml` (required skills 35, experience 20, preferred skills 10, certifications 10, title 10, location/remote 10, employment type 5). The threshold is the stricter of `JOB_MATCH_THRESHOLD` and `minimum_score`.
- Skill and certification detection uses a vocabulary: your profile skills and aliases, search-preference skills and the new `config/skill_vocabulary.yaml` (detection terms only, not claims). Only skills in the candidate profile can ever be reported as matched.
- Requirement classification: section headings (`Required Qualifications:`, `Preferred:`, ...) and inline cues (`must`, `required`, `preferred`, `a plus`). Unlabelled mentions count as required when the posting has no structure, otherwise as preferred.
- Mandatory requirements: required certifications (alternatives such as `CISSP or CCNP` are satisfied by any one), stated years of experience, required degree level. A definite miss gives `INELIGIBLE`; a near miss (1 year or less), clearance, citizenship, visa sponsorship, an unverifiable degree, or a description with no recognisable skills gives `REVIEW`. A score at or above the threshold never overrides any of these.
- Search-preference filters (all give `SKIP`): excluded companies (whole-word), excluded title terms, job age, salary ceiling below `minimum_salary`. The daily cap downgrades further `APPLY` results to `REVIEW`.
- Decision order: filters, mandatory failures, insufficient information, score below threshold, unverified mandatory items, daily cap, `APPLY`.
- Result fields: overall and per-component scores, matched/missing required and preferred skills, matched/missing certifications, `mandatory_requirements_met`, failures, unverified items, filter and review reasons, explanation.
- `app/services/matching_service.py` writes score, recommendation, explanation, missing requirements, full details and status (`ANALYZED`, `REVIEW_REQUIRED`, `SKIPPED`, `INELIGIBLE`) to the job; jobs already past analysis (`PREPARED` and later) are refused with 409.
- `app/api/matching.py`: `POST /matching/analyze` (optional `job_ids`, `limit`; default takes `DISCOVERED` jobs), `POST /matching/analyze/{job_id}`, `GET /matching/{job_id}`. Missing or invalid configuration returns 503.
- `app/services/config_loader.py`: shared YAML loading with example fallback; responses carry `using_example_config`.
- Database schema v2: `jobs.matching_details` and `jobs.analyzed_at`; a v1 database is upgraded in place on start (verified on the local database).
- `skill_vocabulary` is now checked by `validate-config`.

### Phase 3 validation results (2026-09-29)

| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | pass / pass |
| `mypy` (strict, 55 files) | pass |
| `pytest` | **202 passed, 0 failed** (43 new) |
| `run.py validate-config` / `validate-workflows` | pass (3 example-data warnings expected) / 11 of 11 |
| `run.py init-db` on the Phase 2 database | upgraded to schema v2 |

Phase 3 limitations:

- Requirements are found by vocabulary lookup, not language understanding. A skill missing from the vocabulary is invisible to the matcher; extend `config/skill_vocabulary.yaml` for your field.
- Years of experience use the largest stated figure and compare it with `total_years_experience`; per-skill years are not compared.
- The profile has no clearance, citizenship or work-authorisation fields, so those mentions always produce `REVIEW`.
- Degree level is compared only by level (associate, bachelor, master, doctorate), not by field.
- Nothing creates application records yet; the daily cap counts `APPLY` results analysed today.
- Matching was tested with synthetic postings only; scores on real postings are unvalidated.
- The example profile is used when `candidate_profile.yaml` is absent; results then describe the synthetic candidate.

## Phase 4: implemented and tested

- `app/services/resume_tailor.py`: builds a new `.docx` from the candidate profile and reorders only. It moves job-relevant skills to the front and job-relevant bullets to the top of each role. Nothing is added, rewritten or removed, and job requirements the candidate lacks are never added (they are listed as "Not claimed" in the summary).
- The master resume (`master_resume_path` in the profile) is read only, never modified, and its SHA-256 is recorded. Skills, certifications and education that the master does not show are left out with a warning. A role employer or title missing from the master blocks tailoring.
- Every written file is re-read and validated independently of the generator: identity, summary, skills, certifications, education, roles, bullets (same set as the profile), no unexpected sections, and no known skill or certification the candidate does not hold. A failed validation deletes the file and creates no records.
- The synthetic example profile is refused (409), so a resume is never produced from example data.
- `app/services/resume_service.py`: only `ANALYZED` jobs with an `APPLY` result can be tailored. It creates the application record, stores the path, validation result and summary in `tailored_resumes`, and sets the job and application to `PREPARED`. Regeneration is allowed only while `PREPARED` and writes a new version (`-v2`, ...) without overwriting earlier files. Files go to `resumes/generated/<application_id>/`.
- `app/api/resumes.py`: `POST /resumes/tailor/{job_id}` (404 unknown job, 409 not allowed, 422 master unreadable or validation failed, 503 configuration) and `GET /resumes/{application_id}`.
- `MatchingEngine.skills_in` and `unheld_terms` expose vocabulary detection to the tailoring code.

### Phase 4 validation results (2026-09-29)

| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | pass / pass |
| `mypy` (strict, 59 files) | pass |
| `pytest` | **221 passed, 0 failed** (19 new) |

Phase 4 limitations:

- The output is a plain generated layout (Title, Heading 1/2, bullets). The master's own styling, fonts and layout are not reproduced, and the master is used only to verify facts and record its hash.
- The summary paragraph is copied from the profile unchanged; job-specific summary wording would need AI (Phase 9, optional and off by default).
- Verification against the master is text-based (normalised substring), so punctuation differences are ignored (for example `TACACS+` and `TACACS`).
- Verified with synthetic profile and synthetic master `.docx` files generated in tests only. Opening the output in Microsoft Word was not tested, and no PDF is produced.
- The generated resume contains personal data and lives under OneDrive; see PRIVACY.md.

## Phase 5: implemented and tested

- `app/automation/url_guard.py`: the browser opens only plain `http(s)` URLs without credentials on `localhost`, `127.0.0.1` or `::1`, plus hosts you list in `PLAYWRIGHT_ALLOWED_HOSTS` (empty by default). URLs of portals disabled in `portal_settings.yaml` (Workday, Greenhouse, Lever) are refused so those jobs go to manual review.
- `app/automation/worker.py`: one worker thread owns Playwright (its sync API is thread-bound). API threads submit tasks through a queue. Review sessions (browser left open for you) are capped at 3 and closed after `PLAYWRIGHT_SESSION_TIMEOUT` seconds (tested), when the API stops, or when you close the window (implemented, not tested). Startup failure returns a clear 503 instead of hanging.
- Submission guards, always on in this phase: the browser context aborts every request that is not GET/HEAD, an init script blocks `submit`, `form.submit()` and `requestSubmit()` in the page, and the service never calls `click`, presses keys or navigates by itself beyond the initial `goto`.
- `app/automation/form_mapping.py` (pure logic): sensitive-question detection runs first and always wins, using whole-word matching against `sensitive_questions.yaml` (so `age` does not hit `manager` or `language`; camelCase and `snake_case` identifiers are split). A field is filled only from an approved, non-`manual_only` `SAFE_PROFILE`/`SAFE_JOB_SPECIFIC` answer whose match confidence reaches `field_mapping_confidence_threshold` (0.85). Confidence is 1.0 for an exact label, minus 0.03 per extra word, weighted by source (label 1.0, placeholder/name/id 0.95); `autocomplete` hints score 0.98. Ties between different answers are ambiguous and left blank; fields about another person (reference, manager, emergency, ...) are left blank; existing values are never overwritten; dropdowns need an option that equals the approved answer; radio buttons and checkboxes are never selected; only a resume-labelled file field receives the tailored resume.
- Reports store the decision, category, confidence and reason per field, never the answer value.
- `app/automation/portals/`: `BasePortal` interface and the `GenericPortal` adapter (JSON-LD `JobPosting`, then heading/`og:` fallbacks for extraction; label, `aria-label`, `legend`, placeholder, name and id for form fields). CAPTCHA widgets, visible password fields and verification-code prompts are detected and stop the run; nothing is bypassed.
- `POST /jobs/extract` (read-only browser extraction, stored as `BROWSER` jobs with normal duplicate detection), `POST /applications/{id}/form/start`, `GET /applications/{id}/form`, `POST /applications/{id}/form/close`. `form/start` needs a `PREPARED` application with a tailored resume, runs the browser before any database write, then records a `form_runs` row and moves the application to `FORM_STARTED` then `AWAITING_APPROVAL` (or `FAILED` with a reason when blocked). Screenshots (`before`, `after`, or `blocked`) go to `screenshots/<application_id>/`.
- Database schema v3: new `form_runs` table (a v1 or v2 database is upgraded in place).
- New settings: `PLAYWRIGHT_BROWSER_CHANNEL` (`chromium`, `chrome`, `msedge`) and `PLAYWRIGHT_ALLOWED_HOSTS`. When only local pages are allowed the browser starts with `--no-proxy-server`, because system proxy auto-detection made local pages load up to 10 seconds slower on this machine.
- Synthetic fixtures: `tests/fixtures/mock_application.html` (plus job, login and CAPTCHA pages) served by a local test server that records any POST it receives.

### Phase 5 validation results (2026-09-29)

| Check | Result |
|---|---|
| `python -m compileall app scripts tests run.py` | pass |
| `ruff check .` / `ruff format --check .` | pass / pass |
| `mypy` (strict, 74 files) | pass |
| `pytest` | **297 passed, 0 failed** (76 new: 38 mapping, 17 URL guard/portal, 21 real-browser) |
| `run.py validate-config` / `validate-workflows` | pass (3 example-data warnings expected) / 11 of 11 |
| `python run.py serve` (real uvicorn, headless Edge, temporary database) + `POST /jobs/extract` on the synthetic page | job created with company, title, location; a non-local host was refused with 422 |

What the real-browser tests demonstrate (Microsoft Edge, headless, synthetic site only): extraction from JSON-LD, sign-in and CAPTCHA pages stopping the run, seven safe fields filled and the tailored resume attached, every sensitive, legal, demographic, work-authorization, sponsorship, salary, unknown and third-party field left blank, disabled and hidden fields ignored, clicking Submit and a scripted POST both blocked (a control test shows the mock server does record a POST when no guard is present), status history and screenshots recorded, session replacement, close, expiry, the 3-session cap, and refusal of disallowed hosts and non-`PREPARED` applications.

Phase 5 limitations:

- **The bundled Chromium could not be downloaded here (network timeout).** All browser tests ran against the installed Microsoft Edge (`PLAYWRIGHT_BROWSER_CHANNEL=msedge`) in **headless** mode. Bundled Chromium, Chrome, headed mode and `PLAYWRIGHT_SLOW_MO` were not exercised in tests.
- Only the synthetic page has been used. No real employer or portal was contacted; the `generic` adapter stays `validated: false`. Confidence values, the word-count penalty and the third-party term list are heuristics that have not been calibrated on real forms.
- Single-page forms only. No "Next" or "Apply" clicks, so multi-step flows, forms inside iframes or shadow DOM, and custom widgets (for example React-Select, contenteditable) are not filled and may not be seen.
- Radio buttons and checkboxes are never selected, even for harmless questions. Required sensitive or unknown fields stay blank and are listed in `manual_fields`.
- The submission guard also stops you from submitting inside the review browser. The approved submission path was added in Phase 7.
- `AWAITING_APPROVAL` means "filled for review", not "complete": required fields may still need you. Phase 7 re-checks the live session and form state before any approval (done). The review browser lives in the API process, so restarting the API closes it while the status stays `AWAITING_APPROVAL`.
- Screenshots contain the filled-in personal data and sit under OneDrive; see PRIVACY.md.
- Job extraction takes the main-region text when no JSON-LD is present, so boilerplate can end up in the description; quality on real sites is unvalidated.
- `/health` does not launch a browser, so it cannot tell you whether the browser is usable.
- n8n workflow `06_application_preparation` is still a placeholder; wiring these endpoints is Phase 6, and no endpoint has been called from n8n.
- The full test suite now takes about 1.5 minutes because of the browser tests.

## Phase 6: implemented and tested (offline only)

- All eleven files in `n8n/workflows/` are real workflows now (no placeholders). Each has a fixed ID so the master can call stages with Execute Workflow, is imported inactive, uses `$env.JOB_AUTOMATION_API_URL`, and authenticates every call except `GET /health` with the Header Auth credential `Job Automation API Key` (no key in any JSON).
- Master: startup validation (unchanged), then runs `02` list discovered jobs, `04` analysis, `05` resume tailoring, `06` form preparation, `07` approval queue and `09` status snapshot, then a final summary. Manual and daily 08:00 triggers. `01`, `03` need input and are run on their own; `08` is never called.
- Stages are stateless: each reads its work from the API (`GET /jobs?status=...`, `GET /applications?status=...`), so a stage can run alone and does not depend on the previous stage's output.
- `01` Job Discovery: manual (example input), webhook (Header Auth) or called; `input_type` picks `POST /jobs/import/{url,text,json,csv}`, `/jobs/webhook` or `/jobs/extract`. `03` calls `POST /jobs/check` (dry run). `04` calls `POST /matching/analyze` and stops if an APPLY result is below the n8n-side threshold. `05` tailors APPLY jobs up to `MAX_APPLICATIONS_PER_DAY`. `06` takes at most 3 PREPARED applications (the API's review-browser cap) and calls `POST /applications/{id}/form/start`. Failed items are collected in `errors` and do not stop the others.
- `08` Application Submission is disabled: it has no HTTP Request or Execute Workflow node. `10` Error Handler is the error workflow of all others; it formats a report and does nothing else (no retry, no notification).
- New API endpoints `GET /applications` (status, limit, offset) and `GET /applications/{id}` (`app/api/applications.py`, API key required, no resume path or answers returned) so workflows can find work.
- `scripts/validate_n8n_workflows.py` now also checks: HTTP URLs are built from `api_url`, use the credential and set no headers by hand, no URL contains a `/submit` or `/approv` path, workflows are inactive, workflow IDs exist, are unique and every Execute Workflow and error-workflow reference resolves.
- `tests/test_n8n_workflows.py` also checks that every URL and method the workflows call exists in the FastAPI OpenAPI schema, stage order in the master, the shared error handler, the disabled submission workflow and the webhook authentication.
- Browser tests now try Chrome first, then Chromium, then Edge; the 21 browser tests were run on Chrome.

### Phase 6 validation results (2026-09-29)

| Check | Result |
|---|---|
| `python -m compileall app scripts tests run.py` | pass |
| `ruff check .` / `ruff format --check .` | pass / pass |
| `mypy` (strict, 76 files) | pass |
| `pytest` | **322 passed, 0 failed** (25 new) |
| `run.py validate-config` / `validate-workflows` | pass (3 example-data warnings expected) / 11 of 11 |
| `pytest tests/test_browser_forms.py` on Chrome | 21 passed |

Phase 6 limitations:

- **No workflow has been imported into or run by n8n** (n8n, Node and Docker are not installed). Structure, references and endpoints are validated offline, but expression syntax, node parameter shapes (Execute Workflow 1.2, Aggregate 1, Limit 1, Webhook 2, Set 3.4, IF 2.2) and the `alwaysOutputData`/empty-result behaviour are unverified. Expect small fixes on first import.
- Imported workflows carry an empty credential ID; each HTTP Request node needs the credential selected once in the n8n UI.
- Discovery input is manual, webhook or called; there is no RSS or public-API source, and no scheduled discovery.
- Workflows `02` and `03` are read-only views: normalisation and duplicate detection run inside the API at import time. When a stage finds nothing, run on its own it returns no item; inside the master the empty result is handled by `alwaysOutputData`.
- The n8n threshold cross-check in `04` can only flag an inconsistency; the API's own threshold decides.
- `06` needs the API running on Windows with a visible browser to let you review; the Docker API runs headless. The API's review-browser cap is 3, so at most 3 forms are opened per run and the rest wait for the next run.
- No notification is sent on errors or when forms are ready.
- `scripts/export_workflows.py` and `scripts/import_workflows.py` are still not written; the README documents the raw n8n CLI commands.

## Phase 7: implemented and tested (synthetic site only)

- `app/services/approval_service.py` and `app/api/approvals.py`: `POST /applications/{id}/approval/request`, `POST .../approval/confirm`, `POST .../approval/cancel`, `GET .../approval` (all need the API key). No other code path can submit.
- Request: allowed only for `AWAITING_APPROVAL` applications with a `READY_FOR_REVIEW` form run and no earlier submission attempt. It re-checks the live review page in the browser: session still open and on the prepared form's host, no sign-in/CAPTCHA/code prompt, no required or invalid control (the browser's own `validity`, labels only, never values) and exactly one visible submit button. A portal on a non-local host must be `validated: true` in `portal_settings.yaml` (none is), so real portals are refused for submission. On success it returns the exact required text `SUBMIT <application_id>`, a random single-use token (only its SHA-256 is stored), the expiry (`APPROVAL_TOKEN_EXPIRY`, default 3600 s) and a review summary (counts and field labels, no answers). A newer request supersedes the older token.
- Confirm: rejects (403, with an audit row) unknown, other-application, already-used and expired tokens and any text that is not exactly `SUBMIT <application_id>` (case, spacing and extra characters all rejected); 3 wrong texts void the token. A page that is no longer ready returns 409 without consuming the token. Otherwise the token is consumed with an atomic conditional `UPDATE` before the browser is touched (so a failure or crash cannot be replayed), `approved_at` is set, and a per-application lock blocks concurrent confirms.
- Submission (`submit_task`): re-inspects the page, takes a `before-submit` screenshot, sets `window.__jaAllowSubmit`, clears `GuardState.block_submissions` with `submit_budget=1` (the route guard aborts every further non-GET and any non-GET to a disallowed host), clicks the single marked submit button, waits for the page, then restores every guard. `SUBMITTED` is recorded only if the submit request returned status < 400; the confirmation number is stored only when the page shows one (otherwise the note says no confirmation text was found). A click that sent nothing, or a page that was no longer ready, leaves the application `AWAITING_APPROVAL` (`approved_at` cleared, new approval needed). A 4xx/5xx or missing response, or a browser timeout, sets `FAILED` with "check the portal before trying again"; `form/start` then refuses to reopen the form (duplicate-submission protection). On success the review browser is closed.
- `python run.py approve <application_id>` (`scripts/approve_application.py`): interactive terminal only; prints the review summary and asks you to type the exact text; anything else cancels the approval. n8n workflows never call these endpoints: the validator rejects `/submit` and `/approv` URLs and a test asserts no workflow file contains `/approval`. Workflow 07 lists the queue and 08 stays disabled; their notes were updated.
- `GuardState` gained `submit_budget` and `submitted_requests`; `BrowserHost.live_session()` returns the open session and restarts its expiry clock; the radio-group `has_value` in field discovery now reflects any checked option.
- `MAX_TEXT_ATTEMPTS = 3`; all approval events are recorded in `approval_attempts` (`REQUESTED`, `REQUEST_REFUSED`, `REJECTED_*`, `CONFIRM_BLOCKED`, `APPROVED`, `SUBMITTED`, `SUBMIT_*`, `CANCELLED`, `SUPERSEDED`).

### Phase 7 validation results (2026-09-29)

| Check | Result |
|---|---|
| `python -m compileall app scripts tests run.py` | exit 0 |
| `ruff check .` / `ruff format --check .` | All checks passed; 86 files formatted |
| `mypy` (strict) | no issues in 81 source files |
| `pytest` | 368 passed |
| `run.py validate-config` / `validate-workflows` | config: 3 example-file WARNs (expected); workflows: 11/11 OK |

What the tests demonstrate (Chrome, headless, synthetic site only): clicking Submit inside the review browser and a scripted `fetch` POST are blocked before and after an approval is requested; a wrong text sends nothing; the correct text sends exactly one POST to the mock server, marks the application `SUBMITTED`, stores the confirmation number, saves before/after screenshots and closes the browser; replaying the token sends nothing; an empty required field, a closed browser and a second submit button each block the request; a mock server error marks the application `FAILED`, keeps the guard on and prevents reopening the form. Fast tests with a fake browser worker cover expiry, replay, supersession, cancellation, wrong-application tokens, lockout after 3 wrong texts, the non-validated-portal refusal, timeouts and API-key enforcement.

Phase 7 limitations:

- **Submission has only been exercised against the synthetic local site** in Chrome. No real portal was contacted, none is validated, and the guarded click has not been tried on a real multi-step, script-driven or iframe-based form. Only single-page forms with exactly one visible submit button in a `<form>` are supported; anything else is refused and you submit it yourself.
- Success means "the submit request returned status < 400", not "the employer accepted it". Confirmation-text detection is a heuristic (English phrases); a portal that redirects to a page without such text is still recorded as `SUBMITTED` with a note saying no confirmation text was found.
- The typed text proves a deliberate action, not identity: anyone holding `API_KEY` can call the endpoints directly. `run.py approve` requires an interactive terminal but the TTY check cannot stop a determined script.
- `run.py approve` was tested through its function with the API test client; the interactive prompt, TTY check and real HTTP path were not run by hand.
- Required fields that are not HTML-validated (custom widgets, `aria-required` only) are not detected; check the form yourself before typing the approval text.
- Restarting the API closes the review browser and invalidates the approval; the status stays `AWAITING_APPROVAL` until you run form start again. The approval cannot be resumed after a timeout of the review session (`PLAYWRIGHT_SESSION_TIMEOUT` restarts on each approval request/confirm).
- A `FAILED` application after a submission attempt is never reopened automatically; there is no withdraw or manual-resolve endpoint yet.
- Screenshots taken after submission may show personal data and the portal's confirmation page.

## Phase 8: implemented and tested

- `app/services/export_service.py`: `TrackerExporter` reads every job left-joined to its application (`build_row`; the application's values win once one exists, so skipped, ineligible and not-yet-prepared jobs appear with an empty Application ID and the job status) and writes `exports/applications.xlsx` (sheet `Applications`, bold header, frozen header row, autofilter, bounded column widths) and `exports/applications.csv` (UTF-8 with BOM). 25 columns; times are UTC text; the tailored resume is exported as a file name only.
- Each file is written to a temporary name beside the target and swapped in with `os.replace`; a `PermissionError` (workbook open in Excel) becomes `ExportFileLockedError` with nothing partly written and no temp file left.
- Spreadsheet formula injection: `clean_text` strips control characters, caps text at 32,000 characters and prefixes `'` to values starting with `=`, `+`, `-`, `@`, tab, CR or LF; text cells in the workbook are also forced to string type. Numbers (the match score) stay numeric.
- Not exported: approval tokens and audit rows, form answers, screenshots, resume contents, job descriptions, duplicate records.
- `POST /exports/tracker` `{formats?: ["xlsx","csv"]}` (API key required; 422 for unknown, empty or more than two formats; 409 when the file is locked): returns row count, timestamp and file names only. `python run.py export [--format xlsx|csv|both]`.
- n8n workflow 09 now calls `POST /exports/tracker` after its status snapshot and reports the row count and file names; still read-only with respect to the database. Not run in n8n.

### Phase 8 validation results (2026-09-29)

| Check | Result |
|---|---|
| `python -m compileall app scripts tests run.py` | exit 0 |
| `ruff check .` / `ruff format --check .` | All checks passed; 89 files formatted |
| `mypy` (strict) | no issues in 84 source files |
| `pytest` | 388 passed (20 new: 19 export, 1 workflow) |
| `run.py validate-config` / `validate-workflows` | config: 3 example-file WARNs (expected); workflows: 11/11 OK |

What the tests demonstrate: 19 tests in `tests/test_export.py` cover row content for prepared and skipped jobs, reading the workbook back with openpyxl, headers-only export of an empty database, formula payloads (`=`, `+`, `-`, `@`, tab, `HYPERLINK`) in CSV and XLSX with no formula cell produced, control characters, repeated export leaving exactly two files, the API (default and single format, bad formats, API key, 409 on a locked file, no absolute paths in the response) and the CLI.

Phase 8 limitations:

- **Files were not opened in LibreOffice or Google Sheets**; desktop Excel 16.0 was checked once in Phase 10 (below). Formula defusing relies on the leading `'` and the string cell type.
- The `'` prefix is visible in Excel for legitimate values that start with `-`, `+`, `=` or `@`.
- Full snapshot only: each export overwrites the previous files; edits made in Excel are lost and are never imported back. No append mode, no history sheet, no summary sheet.
- Duplicates rejected at import are not in the export. Job descriptions are not exported.
- The whole table is loaded in memory; fine for a personal tracker, not for hundreds of thousands of rows.
- Times are UTC text, not Excel date cells, so they do not sort or filter as dates by type (they sort correctly as text).
- The export sits in `exports\` under OneDrive, which syncs it to the cloud (see PRIVACY.md).
- The workflow 09 change was imported into n8n only in the Phase 10 CLI import (never executed).

## Phase 9: implemented and tested (advisory AI, off by default)

- `AI_ENABLED=false` and `AI_PROVIDER=none` by default: no provider is built, no HTTP client exists, `POST /ai/...` answers 409. New settings: `AI_PROVIDER` (`none`, `mock`, `openai_compatible`), `AI_BASE_URL` (default local Ollama `http://127.0.0.1:11434/v1`), `AI_MODEL`, `AI_API_KEY` (excluded from the settings repr), `AI_TIMEOUT`, `AI_MAX_INPUT_CHARS`, `AI_ALLOW_REMOTE`.
- `Settings.ai_violations()` (part of `safety_violations()`, so startup is refused and `/health` reports it): AI on with provider `none`; `openai_compatible` without a model; an `AI_BASE_URL` that is not http(s), has credentials, is non-local without `AI_ALLOW_REMOTE=true`, or is non-local over plain http.
- `app/ai/schemas.py`: `JobRequest` (company, title, description: all a provider receives), `JobInsights` (summary, required and preferred skills, seniority, red flags) and `SkillComparison`. Model output is untrusted: unknown keys ignored, control characters, links and e-mail addresses stripped, duplicates removed, over-long items dropped, lists and summary capped, non-list skill fields rejected.
- `app/ai/providers.py`: `MockProvider` (offline, deterministic vocabulary matcher) and `OpenAICompatibleProvider` (`POST {AI_BASE_URL}/chat/completions`, temperature 0, JSON mode, posting wrapped in `<job_posting>` tags with the closing tag neutralised and a system prompt to treat it as data). No redirects, no environment proxies, 1 MB response cap, fenced JSON accepted, errors carry only the status code or exception type, never the prompt or body.
- `app/services/ai_service.py`: `AIJobAnalysisService.analyze` refuses when disabled, unknown job (404), or no usable description (422); truncates the description to `AI_MAX_INPUT_CHARS`; caches per provider, model and text (`force=true` re-sends); validates the reply; compares required and preferred skills with the candidate profile locally by exact normalised name or alias (anything unlisted is a gap; nothing is added to any resume or answer); stores the result in the new `ai_analyses` table (schema v4); writes an `audit` event with counts only.
- `app/api/ai.py`: `GET /ai/status`, `POST /ai/jobs/{job_id}/analyze[?force=true]`, `GET /ai/jobs/{job_id}` (API key required; 409 disabled, 404, 422, 502 provider failure or invalid reply, 504 timeout).
- The analysis never touches `jobs`, `applications`, resumes, form runs or approvals (a test snapshots the job and application counts before and after). No n8n workflow calls it; the approval and submission code does not import it.

### Phase 9 validation results (2026-09-29)

| Check | Result |
|---|---|
| `python -m compileall app scripts tests run.py` | exit 0 |
| `ruff check .` / `ruff format --check .` | All checks passed; 95 files already formatted |
| `mypy` (strict) | no issues in 90 source files |
| `pytest` | **434 passed, 0 failed** (46 in `tests/test_ai.py`) |
| `run.py validate-config` / `validate-workflows` | config: 3 expected example-file WARNs; all workflows OK |

What the tests demonstrate: 46 tests in `tests/test_ai.py` cover the off-by-default state and every misconfiguration; hostile model output (links, e-mail addresses, control characters, extra keys such as `action` and `status`, oversized lists, non-list fields) being sanitised and having no effect on the job; provider failures mapping to 502/504 with nothing stored; the request shape, bearer header, delimiter neutralisation, fenced JSON, malformed replies, 500 responses without body leakage, redirects not followed, timeouts, connection errors and the size cap, all against `httpx.MockTransport`; only posting text (no name, e-mail or certification from the profile) reaching the transport; truncation; caching; and API-key enforcement.

Phase 9 limitations:

- **No real model or endpoint was ever contacted.** `OpenAICompatibleProvider` was exercised only through a fake transport. Real Ollama, LM Studio, vLLM or OpenAI behaviour (JSON-mode support, response shape differences, latency, model quality, hallucinated skills) is unverified.
- The `mock` provider is a keyword matcher, not AI. Its output is only as good as `skill_vocabulary.yaml`.
- Prompt injection is mitigated, not solved: the prompt marks the posting as data and the output is validated, but a model can still be misled into a wrong summary, skill list or red flag. That is why the result changes nothing and is labelled advisory.
- Skill comparison is exact name or alias matching after normalisation: a model that says "K8s" is a gap unless the profile lists "K8s" or "Kubernetes" with that alias, and a related but differently named skill is not credited.
- It is a single-shot analysis of one job. No batch endpoint, no cover letters, no resume rewriting, no answers to application questions (by design: those would need truthfulness guarantees this phase does not provide).
- A non-local provider means job text leaves the machine. Docker cannot use a plain-http server on the host because non-local endpoints must be https.
- The provider call runs synchronously while the request holds its database session; a slow model blocks that request for up to `AI_TIMEOUT`.
- `using_example_config` is true when the example candidate profile supplied the comparison.

## Phase 10: final end-to-end validation (synthetic site only)

`tests/test_end_to_end.py` starts a real `run.py serve` process (temporary project root, API key set, headless Edge/Chrome) and drives it over HTTP against the local mock site:

- `/health` reports approval required, automatic submission off, AI off, no violations; unauthenticated calls get 401.
- Import (one duplicate detected), matching (weak job not `APPLY`, tailoring it refused with 409), tailoring (validation passed, file created), form preparation (`READY_FOR_REVIEW`, `AWAITING_APPROVAL`, nothing posted).
- A forged token and wrong approval texts are refused with nothing posted; the exact `SUBMIT <application_id>` submits once; a replay is refused; the site received exactly one POST.
- Tracker export (CSV and XLSX) shows `SUBMITTED` for the submitted job and no application for the weak one.
- The database records the status history and approval attempts in order, the approval is consumed, and the raw token appears in neither the database nor any log.
- The server refuses to start with `AUTOMATIC_SUBMISSION_ENABLED=true` or `MANUAL_APPROVAL_REQUIRED=false`.

### Phase 10 validation results (2026-09-29)

| Check | Result |
|---|---|
| `python -m compileall app scripts tests run.py` | exit 0 |
| `ruff check .` / `ruff format --check .` | All checks passed; 97 files already formatted |
| `mypy` (strict) | no issues in 92 source files |
| `pip check` | no broken requirements |
| `pytest` | **454 passed, 0 failed** (3 in `tests/test_end_to_end.py`, 16 in `tests/test_docker_config.py`) |
| `run.py validate-config` / `validate-workflows` | config: 3 expected example-file WARNs; all 11 workflow files OK |

The end-to-end tests were also re-run alone after a later edit to that file (3 passed).

Native container simulation (2026-09-29, no Docker engine): only the files the Dockerfile copies, a clean venv and `pip install -r requirements.txt` (exit 0, `pip check` clean), then `uvicorn app.main:app_factory` with the compose environment (`API_HOST=0.0.0.0`, headless, `API_KEY` set). The Dockerfile HEALTHCHECK request returned 200, `/health` reported `startup_ok` with safe flags and writable directories, `run.py serve` without `API_KEY` on `0.0.0.0` exited 1, and config validation passed. The first run failed with `No module named 'httpx'`: the app imports `httpx` but `requirements.txt` only listed `httpx2`. Fixed by adding `httpx>=0.27`. This does not test the image build, the Linux base image, Chromium in the image, volumes or the compose network.

Desktop Excel check (2026-09-29): a synthetic export (2 rows, one with a `=HYPERLINK(...)` title) was opened read-only in Excel 16.0 through COM automation. The sheet is `Applications`, range A1:Y3, header row frozen, all 25 headers correct. The formula payload is stored as text (not a formula) and displays with a visible leading `'`. Only this one Excel version and one file were checked; no Excel-side editing, filtering or saving was tried.

This validates the Python service, the approval gate and the tracker export together. It does not validate anything listed below.

## Not verified / known limitations

- **n8n workflows were imported but never executed.** On 2026-09-29 `n8n import:workflow --separate` (n8n 2.41.3, the version pinned in `docker-compose.yml`, installed with a portable Node 24 outside the project) imported all 11 files into a throwaway database ("Successfully imported 11 workflows", ids `jaWf00Master` to `jaWf10ErrHandler` listed by `list:workflow`). The n8n server, the editor, the credential setup, the schedule triggers and every node's runtime behaviour were not run. Node `typeVersion` values (IF 2.2, Set 3.4, HTTP Request 4.2, Schedule Trigger 1.2) were accepted by the importer, which does not prove they behave correctly.
- **Docker build and `docker compose up` were not run locally.** Docker Desktop refused to start on this machine ("Virtualization support not detected", WSL not installed), and it was later removed. Substitutes: `tests/test_docker_config.py` (static checks of the Dockerfile, compose file, `.dockerignore` and `requirements.txt` coverage) and a native simulation (Dockerfile COPY set plus a clean venv install). The simulation found that `httpx` was missing from `requirements.txt` (it would have crashed the image at start-up); that is fixed. `.github/workflows/docker-check.yml` runs the real build, `compose up`, health, non-root, Chromium and n8n import checks on GitHub; its result is recorded in the next item.
- **GitHub Actions `docker-check` passed on commit `f83a869` (run #4, 2m 9s, ubuntu-latest).** It ran hadolint, `compose config`, the refusal to start without each secret, the image build and `compose up --wait` for both services, `/health` with `startup_ok` true and safe flags, the API running as uid 1000, `/jobs` returning 401 without a key, headless Chromium launching in the image, n8n reaching the API by service name, and `n8n import:workflow` (11 workflows listed). Earlier runs found and led to three fixes: `/health` treated the read-only config mount as unwritable, the secrets check depended on which missing variable compose named first, and the import raced n8n's start-up migrations. Not covered by CI: the n8n server executing any workflow, real portals and real AI models.
- In a container the review browser is headless, so form review for approval should run on the host, not in Docker.
- Chromium is not installed (the download timed out in Phase 5); an installed Edge was used instead, see Phase 5 limitations. `/health` only checks the Python package.
- No community Playwright node has been evaluated or installed.
- `scripts/export_workflows.py` and `scripts/import_workflows.py` are deferred to Phase 6 (README documents the raw n8n CLI commands).
- `MAX_APPLICATIONS_PER_DAY` (used by n8n workflow 05 only) is parsed and validated but not used by the API (`PLAYWRIGHT_*` since Phase 5, `APPROVAL_TOKEN_EXPIRY` since Phase 7, `AI_*` since Phase 9).
- `sensitive_questions.yaml` is matched on word boundaries since Phase 5; keywords of five or more letters also match longer forms (`relocat` matches `relocation`), shorter ones (`age`, `visa`, `race`) match only as whole words.
- `.env` and the master resume do not exist yet (user-supplied). `.env` is optional for local runs; the `.venv`, `data\job_tracker.db` and `logs\` created during validation are git-ignored.
- The project is in a OneDrive folder; see PRIVACY.md.

## Deviations from the brief

- Project files live at the workspace root instead of a nested `job-application-automation/` folder.
- Files belonging to later phases (`app/ai`, most `app/api` routers, `app/models/approval.py`, DOCX templates, phase-specific tests) are created in their phases instead of as empty placeholders. Browser code lives in `app/automation/` (with `portals/`) instead of the brief's `app/browser` and `app/portals`.
- Added `pydantic[email]` and `httpx2` (starlette's test client now requires it) to requirements.

## Unsupported functionality (current)

Nothing further is planned. n8n workflows import into n8n 2.41.3 but were never executed there. No portal is supported or validated, submission has only ever been exercised against the synthetic local site, and no real AI model was ever contacted.
