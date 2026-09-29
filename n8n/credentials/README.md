# n8n credentials

No credentials are stored in this repository. Create them in the n8n UI (Credentials > Create):

| Credential | Type | Purpose | Used from |
|---|---|---|---|
| Job Automation API Key | Header Auth (`X-API-Key` = your `API_KEY` from `.env`) | Authenticates n8n to the FastAPI service | Every HTTP Request node except `GET /health` |
| Job Discovery Webhook Key (optional) | Header Auth (any long random header name and value) | Protects the webhook in workflow 01 | Workflow 01 Webhook Trigger |
| AI provider key (optional) | Not used | The API reads `AI_API_KEY` from `.env`; no workflow calls the AI endpoints, so nothing goes into n8n | none |
| Notification (optional) | Email / Slack / etc. | Completion notifications | Notification node |

Rules:

- Never paste secrets into workflow JSON, Set nodes or Code nodes.
- Never store job-portal passwords. The system does not automate logins.
- `n8n/credentials/*.json` is git-ignored in case you export credentials; do not commit exports.
- Workflows are imported with an empty credential ID; open each HTTP Request node once and pick the credential you created. `python run.py validate-workflows` rejects any HTTP node that sets headers by hand.
