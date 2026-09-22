# mercury-config

Private. Holds the real `mercury.yaml` for one deployment of the public Mercury image and the workflow that deploys it. The public repo ships the image and a sample config; this repo is the only place the chat ID, the repo list and the schedule live.

## Layout

```
mercury.yaml                 the config, copied from the public sample and filled in
.github/workflows/deploy.yml deploys the pinned image with this config
```

## Rules

- The image tag in `deploy.yml` is pinned to a commit tag from the public repo and bumped by hand. Never `latest`. A public commit must not change what talks to the phone before it has been read.
- Secrets are Actions secrets here and Container Apps secrets in Azure. Nothing secret is in `mercury.yaml`.
- Every push to `main` deploys. There is no staging; the config is small enough to read before pushing.

## Secrets this repo's Actions need

`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` (OIDC, same federated credential pattern as the public repo), `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `MERCURY_BEARER_TOKEN`, `MERCURY_GITHUB_TOKEN`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`.
