# Job Application Automation (preparation mode)

> **Status: Phase 9 of 10 complete (foundation, job ingestion, matching, resume tailoring, browser form preparation, n8n workflows, manual approval and guarded submission, tracker export, optional advisory AI).** See [PROJECT_STATUS.md](PROJECT_STATUS.md) for exactly what is implemented, tested and still missing. Sections marked *(planned: Phase N)* describe behaviour that does **not exist yet**.

## 1. Purpose

A local, modular system that finds jobs, removes duplicates, scores them against your profile, tailors a truthful resume, prepares (never submits on its own) an application form with Playwright, waits for your explicit approval, and records everything in a SQLite tracker.

## 2. Architecture

```
n8n (orchestration) ──HTTP──▶ FastAPI service ──▶ SQLite tracker
                                  │
                                  └──▶ Playwright (Chromium) ──▶ job portal (stops before Submit)

Discover → Normalize → Duplicate check → Match analysis (+ optional AI)
   score < 75 → SKIPPED │ mandatory requirement missing/unknown → REVIEW/INELIGIBLE
   score ≥ 75 → Tailor resume → Prepare application → AWAITING_APPROVAL
   "SUBMIT <application_id>" (exact, single use, time limited) → Submit → Tracker update
```

Layout differences from the original brief: the project lives at the workspace root (no extra `job-application-automation/` folder), and Phase-specific modules (`services/`, `browser/`, `portals/`, `ai/`, most of `api/`) are created in the phase that implements them rather than as empty placeholders.

## 3. Important safety limitations

- Automatic submission is disabled. `AUTOMATIC_SUBMISSION_ENABLED` must be `false` and `MANUAL_APPROVAL_REQUIRED` must be `true`; the API refuses to start otherwise, and the master n8n workflow stops.
- No CAPTCHA, MFA, login, rate-limit, robots or bot-detection bypass. No portal credentials are requested or stored.
- No portal is validated. Do not assume Workday, Greenhouse, Lever, LinkedIn or Indeed works.
- Only synthetic data is used in tests. Test live portals only after you explicitly approve a specific one.

## 4. Prerequisites

- Windows 10/11, Python 3.11+ (developed with 3.12)
- n8n (Docker or npm) — needed from Phase 6
- Docker Desktop (optional)

## 5. Docker setup

```
copy .env.example .env
# edit .env: set API_KEY and N8N_ENCRYPTION_KEY to long random values
docker compose up -d
```

n8n: http://localhost:5678. API: http://127.0.0.1:8000/health. Both bind to `127.0.0.1` only. The container browser is headless (no display); to *watch* the browser during review, run the API locally on Windows (section 6) and point n8n at `http://host.docker.internal:8000`. **The Docker files have not been built or run in this environment (Docker was not installed).**

## 6. Windows local setup

Command Prompt:

```
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
```

PowerShell:

```
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
```

Then:

```
copy .env.example .env
python scripts\initialize_database.py      # create data\job_tracker.db and folders
python scripts\validate_configuration.py   # validate .env safety flags and config\*.yaml
python run.py serve                        # start FastAPI on 127.0.0.1:8000
```

Check it: `curl http://127.0.0.1:8000/health` (`startup_ok` must be `true`).

Other commands:

```
python scripts\validate_n8n_workflows.py   # offline structural check of n8n JSON
python -m pytest                           # tests
python -m ruff check .                     # lint
python -m mypy                             # type check
python run.py --help
```

## 7. n8n installation

Docker (recommended): included in `docker-compose.yml`. Without Docker (requires Node.js 20+):

```
npm install -g n8n
set N8N_BLOCK_ENV_ACCESS_IN_NODE=false
n8n start
```

## 8. Workflow import

Import `n8n/workflows/*.json` through the n8n UI (Workflows → Import from file), or with the CLI:

```
n8n import:workflow --separate --input=n8n/workflows
```

Docker: `docker compose exec n8n n8n import:workflow --separate --input=/workflows`.

Before running anything, create two credentials in the n8n UI (see [n8n/credentials/README.md](n8n/credentials/README.md)): **Job Automation API Key** (Header Auth, `X-API-Key`) and, only if you use the webhook in workflow 01, **Job Discovery Webhook Key**. Imported workflows keep an empty credential ID, so open each HTTP Request node once and select the credential.

| Workflow | What it does |
|---|---|
| `master_job_application_workflow` | Startup validation (`GET /health`, safety flags), then runs 02, 04, 05, 06, 07 and 09 in order. Manual and daily 08:00 triggers. |
| `01` Job Discovery | Manual, authenticated webhook or called: imports URL, text, JSON, CSV, webhook payloads, or extracts a page read-only. Input-driven, so the master does not run it. |
| `02` Job Normalization | Lists DISCOVERED jobs (normalisation itself happens in the API at import). |
| `03` Duplicate Check | Dry-run `POST /jobs/check` for one job. Input-driven. |
| `04` Job Analysis | `POST /matching/analyze`; stops if an APPLY result is below the n8n threshold. |
| `05` Resume Tailoring | Tailors APPLY jobs (at most `MAX_APPLICATIONS_PER_DAY`). |
| `06` Application Preparation | Starts up to 3 PREPARED applications' forms for review. Never submits. |
| `07` Manual Approval | Lists AWAITING_APPROVAL applications. Approval itself is done by you with `python run.py approve <application_id>`, never by a workflow. |
| `08` Application Submission | **Disabled.** No HTTP nodes; not called by anything. |
| `09` Tracker Update | Status counts, then `POST /exports/tracker` writes the Excel and CSV files. |
| `10` Error Handler | Error workflow of all the others; formats a report, retries and sends nothing. |

Workflows call each other by fixed IDs (`jaWf02Normalize`, ...), so import all of them. All are imported inactive; nothing runs on a schedule until you activate the master workflow. Each stage reads its work from the API instead of from the previous stage, so stages can be run on their own.

**Import into a live n8n instance has not been tested** (n8n, Node and Docker are not installed here). Only offline structural validation ran (`python run.py validate-workflows`), plus tests that every URL the workflows call exists in the FastAPI app. Node parameter shapes (Execute Workflow 1.2, Aggregate, Limit, Webhook 2, Set 3.4) follow n8n 2.x but have not been opened in the editor; expect to fix small details on first import.

## 9. Community-node verification

No n8n community Playwright node is used or required. Nothing has been installed or verified. The reliable path is `n8n → HTTP Request → FastAPI → Python Playwright`. A community node will be adopted only if it passes the compatibility and local-test criteria in the brief *(planned: Phase 5/6)*.

## 10. FastAPI fallback

This is the default design. n8n never drives the browser directly; it calls these endpoints (all need `X-API-Key` when `API_KEY` is set):

| Endpoint | Purpose |
|---|---|
| `POST /jobs/extract` `{url, company?, title?}` | Open a job page read-only, read the posting (schema.org JSON-LD, then headings) and store it as a `BROWSER` job. 409 when the page needs sign-in, CAPTCHA or a code. |
| `POST /applications/{id}/form/start` `{application_url?}` | For a `PREPARED` application: open the form, fill approved safe fields, attach the tailored resume, take before/after screenshots, leave the browser open for you, set `AWAITING_APPROVAL`. **Never submits.** |
| `POST /applications/{id}/approval/request`, `POST .../approval/confirm`, `POST .../approval/cancel`, `GET .../approval` | The approval gate (section 17). The only route to a submission. Not called by any workflow. |
| `GET /applications/{id}/form` | Latest field-by-field report (no answer values) and whether the review browser is still open. |
| `GET /applications?status=&limit=` and `GET /applications/{id}` | Read-only application summaries (used by workflows 06, 07 and 09; no resume path or answers). |
| `POST /exports/tracker` `{formats?: ["xlsx", "csv"]}` | Write the tracker files (section 19). Returns the row count and file names only. 409 when the workbook is open in Excel. |
| `POST /applications/{id}/form/close` | Close the review browser; the status is unchanged. |

Safety rules enforced in code: only `localhost` is opened unless you add hosts to `PLAYWRIGHT_ALLOWED_HOSTS`; portals disabled in `portal_settings.yaml` (Workday, Greenhouse, Lever) are refused; the service never clicks, presses Enter or signs in; the browser context aborts every non-GET request and blocks page-level form submission; sensitive, legal, demographic, work-authorization, sponsorship, salary, clearance, relocation and unknown questions are left blank and listed in `manual_fields`; radio buttons and checkboxes are never selected; a field is filled only when the match confidence reaches `field_mapping_confidence_threshold` (0.85).

## 11. Playwright installation

`python -m playwright install chromium` (or `python scripts\install_playwright.py`). If the download is blocked on your network, use an installed browser instead: set `PLAYWRIGHT_BROWSER_CHANNEL=msedge` (or `chrome`) in `.env`. Set `PLAYWRIGHT_HEADLESS=false` (the default) to watch and finish the form yourself.

## 12. Candidate profile setup

```
copy config\candidate_profile.example.yaml config\candidate_profile.yaml
```

Edit every value. The shipped file is a **synthetic, editable example** for a network-security engineer. Real profile files are git-ignored. `python scripts\validate_configuration.py` validates the schema.

## 13. Master resume setup

Put your master resume at `resumes\source\master_resume.docx` (path configurable in the profile). It is the only authoritative source and is never overwritten.

`POST /resumes/tailor/{job_id}` (only for `ANALYZED` jobs with an `APPLY` result) writes `resumes\generated\<application_id>\<first>-<last>-resume.docx`. The resume is built from `candidate_profile.yaml`, reordered for the job (relevant skills and bullets first) and validated against the profile and the master: nothing is added, rewritten or removed, and skills, certifications or education that the master does not show are left out with a warning. If validation fails the file is deleted and a 422 explains why. Regenerating creates a new `-v2` file. `GET /resumes/{application_id}` lists versions with their validation results. The example profile is refused, and the output uses a plain generated layout rather than the master's own styling.

## 14. Job-source configuration

Copy `config\search_preferences.example.yaml` to `config\search_preferences.yaml` and edit it (its filters are applied by the matching engine).

Implemented sources (Phase 2): manual URL (needs company and title until browser extraction exists), webhook, JSON, CSV and pasted job text. Not yet implemented: approved RSS feeds, public job APIs, browser extraction. No uncontrolled scraping.

```
python run.py import-jobs jobs.csv          # or jobs.json
```

API (send `X-API-Key` if `API_KEY` is set; interactive docs at `/docs`):

| Endpoint | Purpose |
|---|---|
| `POST /jobs/import/url` | `{url, company, title, ...}` |
| `POST /jobs/import/text` | `{text, company?, title?, job_url?}`; `Title:`/`Company:` header lines are recognised |
| `POST /jobs/import/json` | one job, a list, or `{"jobs": [...]}` |
| `POST /jobs/import/csv` | `{csv_text}` |
| `POST /jobs/webhook` | same shapes as JSON, tagged as webhook |
| `POST /jobs/check` | fingerprint and duplicate-check without storing |
| `GET /jobs`, `GET /jobs/{job_id}` | list and look up stored jobs |

Duplicates are detected by fingerprint, normalised URL, external ID, company + title (same or unknown location), description hash, existing application and, if `FUZZY_DUPLICATE_THRESHOLD` > 0, fuzzy description similarity. A duplicate is recorded with its reason and the original job and is never analysed, tailored or opened again.

## 15. Match-score configuration

`config\scoring_weights.yaml` (weights must sum to 100; default minimum 75). A score ≥ 75 never overrides a missing mandatory requirement; unknown mandatory requirements produce `REVIEW`. The effective threshold is the stricter of `JOB_MATCH_THRESHOLD` and `minimum_score`.

`config\skill_vocabulary.yaml` lists the skills and certifications the matcher can recognise in postings (detection only; your profile decides what you have). Add terms for your field, otherwise they are invisible to matching.

| Endpoint | Purpose |
|---|---|
| `POST /matching/analyze` | `{job_ids?, limit?}`; analyses `DISCOVERED` jobs when `job_ids` is omitted |
| `POST /matching/analyze/{job_id}` | analyse one job |
| `GET /matching/{job_id}` | stored result of the last analysis |

Recommendations: `APPLY` (score at or above the threshold, nothing unverified), `REVIEW` (needs a human decision), `SKIP` (filtered or below threshold), `INELIGIBLE` (a mandatory requirement is definitely not met). Each result includes a plain-language explanation.

## 16. AI provider configuration

Disabled by default (`AI_ENABLED=false`, `AI_PROVIDER=none`). When enabled it is **advisory only**: it reads one stored job posting and returns a short summary, the required and preferred skills it finds, a seniority hint and red flags. It never changes the match score, recommendation, status, resume or any form answer, is not used by any workflow, and has no role in approval or submission.

| Setting | Meaning |
|---|---|
| `AI_ENABLED` | Master switch. |
| `AI_PROVIDER` | `none`, `mock` (offline, deterministic keyword finder for tests and demos) or `openai_compatible` (a `/chat/completions` endpoint such as Ollama, LM Studio, vLLM or OpenAI). |
| `AI_BASE_URL`, `AI_MODEL`, `AI_API_KEY` | `openai_compatible` only. Default URL is a local Ollama at `http://127.0.0.1:11434/v1`; the model is required; the key is optional and stays in `.env`. |
| `AI_ALLOW_REMOTE` | Must be `true` before a non-local `AI_BASE_URL` is accepted (which must also be https). Otherwise the service refuses to start. |
| `AI_TIMEOUT`, `AI_MAX_INPUT_CHARS` | Seconds to wait (default 60) and how much of the description is sent (default 8000). |

Endpoints (API key required): `GET /ai/status`, `POST /ai/jobs/{job_id}/analyze[?force=true]` (409 when AI is off, 404 unknown job, 422 no usable description, 502 provider failure or invalid reply, 504 timeout) and `GET /ai/jobs/{job_id}` (last stored result). Results are cached per job, provider, model and text; `force=true` sends the posting again.

Only the job title, company and description are sent to the provider, never your profile, resume, answers or contact details. The comparison with your profile happens locally: a skill counts as covered only when your profile lists that name or an alias exactly (case and punctuation ignored); anything else is reported as a gap, and nothing is ever added to your resume or answers. To use a local model: install Ollama, pull a model, then set `AI_ENABLED=true`, `AI_PROVIDER=openai_compatible`, `AI_MODEL=<model name>` in `.env`. From Docker, the API cannot reach a plain-http server on the host (a non-local endpoint must be https and needs `AI_ALLOW_REMOTE=true`), so run the API on Windows when you want a local model.

## 17. Manual approval process

1. Run the form preparation (workflow 06 or `POST /applications/{id}/form/start`). The review browser stays open with the safe fields filled. Submitting inside it is blocked.
2. In that window, fill every field that was left for you (sensitive, legal, salary, work-authorization questions and anything else) and check the form.
3. In an interactive terminal run `python run.py approve APP-20260929-0001`. It asks the API to re-check the live page: the browser must still be open on the prepared site, no sign-in, CAPTCHA or code prompt, no required or invalid field (checked with the browser's own validation), and exactly one submit button. It then prints a summary and asks you to type exactly `SUBMIT <application_id>` (for example `SUBMIT APP-20260929-0001`). Anything else cancels.
4. The API accepts the text only with the single-use token it issued for that request. The token is stored as a hash, lasts `APPROVAL_TOKEN_EXPIRY` seconds (default 3600), is consumed atomically before the browser is touched, and is voided by a newer request, by cancelling, or after 3 wrong texts. Every request, rejection and outcome is recorded (`GET /applications/{id}/approval`).
5. The service re-checks the page again, lifts the submit guard for one click and a budget of one network request, clicks the single submit button, then restores the guard. `SUBMITTED` is recorded only when that request returned a status below 400; the confirmation number is stored when the page shows one, otherwise the note says no confirmation text was found. A server error or timeout marks the application `FAILED` with "check the portal", and the form is not reopened automatically, to avoid a duplicate submission.

Refusals: a portal on a non-local host must be marked `validated: true` in `config\portal_settings.yaml` (none is), otherwise you submit that application yourself. `run.py approve` refuses to run without an interactive terminal. n8n workflows never call these endpoints (the validator and tests enforce it). Anyone who holds `API_KEY` can still call the endpoints directly; the typed text is a deliberate-action check, not proof of identity.

## 18. Job-tracker usage

SQLite at `data\job_tracker.db` (schema v4: jobs, duplicate records, applications, status history, tailored resumes, form runs, approvals, approval attempts, AI analyses). Statuses: DISCOVERED, DUPLICATE, ANALYZED, SKIPPED, INELIGIBLE, REVIEW_REQUIRED, PREPARED, FORM_STARTED, AWAITING_APPROVAL, SUBMITTED, FAILED, WITHDRAWN. `SUBMITTED`, `WITHDRAWN` and `DUPLICATE` are final.

## 19. Excel and CSV exports

`exports\applications.xlsx` (sheet `Applications`) and `exports\applications.csv` (UTF-8 with BOM, so Excel opens it directly). Create them with `python run.py export [--format xlsx|csv|both]`, `POST /exports/tracker`, or n8n workflow 09.

- One row per job, with the application's fields when one exists (jobs that were skipped, ineligible or not yet prepared have an empty Application ID). It is a full snapshot: each export overwrites the previous files, and manual edits to them are not read back.
- Columns: application and job IDs, company, title, location, employment and workplace type, source, portal, job URL, discovered date, match score, recommendation, matching explanation, missing requirements, tailored resume **file name** (no folder), status, started/awaiting-approval/approved/submitted times, confirmation number, failure reason, notes, last update. Times are UTC text.
- Not exported: approval tokens or audit rows, form answers or field values, screenshots, resume contents, job descriptions.
- Cell text is defused against spreadsheet formula injection: values starting with `=`, `+`, `-`, `@`, tab or carriage return get a leading `'`, control characters are removed and text is capped at 32,000 characters. A legitimate value such as `-5` in a text field therefore shows a leading apostrophe.
- Files are written to a temporary name and swapped in atomically. If `applications.xlsx` is open in Excel the export fails with a message to close it (nothing is partly written).
- Duplicates rejected at import are recorded in the database (`duplicate_records`) but are not exported.

## 20. Supported portals

None validated. `config\portal_settings.yaml` records this: `generic` is experimental and has been exercised only against the synthetic page `tests\fixtures\mock_application.html`; Greenhouse and Lever are not validated, Workday is unsupported.

## 21. Unsupported portal operations

Login automation, CAPTCHA/MFA handling, multi-step authenticated flows (for example Workday), and answering work-authorization, sponsorship, salary, demographic, legal, clearance, relocation, conflict-of-interest or unknown questions. These are always left for you.

## 22. Troubleshooting

| Symptom | Fix |
|---|---|
| `Refusing to start: ... must be ...` | Restore the safe defaults in `.env`. |
| `/health` shows `startup_ok: false` | Read `database`, `directories`, `safety.violations`; run `python scripts\initialize_database.py`. |
| 401 from API | Send `X-API-Key` equal to `API_KEY`. |
| n8n `access to env vars denied` | Set `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` for n8n. |
| n8n in Docker cannot reach the local API | Use `http://host.docker.internal:8000`. |
| 503 `Could not start the browser` | Run `python scripts\install_playwright.py` or set `PLAYWRIGHT_BROWSER_CHANNEL=msedge`. |
| 422 `Host ... is not allowed` | Only localhost is opened by default; add a host you trust to `PLAYWRIGHT_ALLOWED_HOSTS` deliberately. |
| 409 `Manual action required` / status `FAILED` after form start | The page needs sign-in, a CAPTCHA or a code. Handle the application yourself; nothing was filled. |

## 23. Testing

`python -m pytest`. Tests use only synthetic candidate/job data and never contact a real portal.

## 24. Privacy

See [PRIVACY.md](PRIVACY.md). Screenshots and logs may contain personal data.

## 25. Security

See [SECURITY.md](SECURITY.md).

## 26. Adding a new portal

Implement `BasePortal` (`app\automation\portals\base.py`: `extract_job`, `discover_fields`), add an entry to `config\portal_settings.yaml` with `validated: false`, validate against a synthetic page, and only then mark it validated. Only the generic adapter exists today.

## 27. Recovery after interrupted execution

Application state is persisted in SQLite with a status history. Submitted applications cannot change state, which prevents duplicate resubmission. If the API restarts, the review browser closes while the status stays `AWAITING_APPROVAL`: run form start again. An application whose submission was attempted (`FAILED` after approval) is not reopened; check the portal.

## 28. Uninstall and delete personal data

1. `docker compose down -v` (removes volumes) if you used Docker.
2. Delete: `data\`, `resumes\`, `screenshots\`, `exports\`, `logs\`, `config\candidate_profile.yaml`, `config\search_preferences.yaml`, `config\safe_answers.yaml`, `.env`, `.venv\`.
3. In n8n, delete the imported workflows, credentials and execution history (executions may hold job data).
4. OneDrive: also empty the OneDrive recycle bin and check version history for the project folder.
