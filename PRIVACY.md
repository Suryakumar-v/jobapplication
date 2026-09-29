# Privacy

All data stays on your machine. Nothing is sent to any third party unless you enable an AI provider (disabled by default) or n8n nodes you add yourself.

## Personal data stored locally

| Location | Contents |
|---|---|
| `config\candidate_profile.yaml`, `safe_answers.yaml` | Your name, contact details, work history |
| `resumes\source\`, `resumes\generated\` | Master and tailored resumes |
| `data\job_tracker.db` | Jobs, applications, status history, approval audit trail |
| `screenshots\<application_id>\` | Full-page screenshots of application forms, including `before-submit` and `after-submit` pages |
| `exports\` | Tracker exports: company, title, job URL, match explanation, notes, confirmation numbers and tailored-resume file names. No tokens, form answers, screenshots or resume contents |
| `logs\` | Redacted operational and audit logs |
| n8n data volume | Workflow definitions, credentials (encrypted), execution history |

## Warnings

- **Screenshots may contain personal data** (name, address, phone, email, resume content and any typed answers). Review them before sharing and delete them when no longer needed.
- Logs are redacted on a best-effort basis; do not share them without review.
- n8n stores execution data, which can include job descriptions and API responses.
- This project folder is under OneDrive: files (including `.env`, resumes and screenshots) sync to the cloud. Consider moving the data folders outside OneDrive or excluding them from sync.

## Never stored or logged

Passwords, API keys, raw approval tokens, full cookies, browser storage state, sensitive answers (work authorization, demographic, etc.), complete resumes in logs.

## Deletion

See "Uninstall and delete personal data" in [README.md](README.md).

## AI providers

AI is off by default. When you enable it, only the job's title, company and description are sent to the provider (never your profile, resume, answers, contact details or approval data), and only when you call `POST /ai/jobs/{job_id}/analyze`. With `AI_PROVIDER=mock` nothing leaves the machine. With `openai_compatible` the default endpoint is a local server (`http://127.0.0.1:11434/v1`); a non-local endpoint is refused at startup unless `AI_ALLOW_REMOTE=true` and it uses https. Review a remote provider's data-handling terms first. The provider's reply is stored in `data\job_tracker.db` (table `ai_analyses`). `AI_API_KEY` lives only in `.env`.
