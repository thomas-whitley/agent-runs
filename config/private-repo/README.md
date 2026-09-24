# mercury-config

Private. Holds the real `mercury.yaml` for the one deployment of the public `thomas-whitley/agent-runs` image, and the workflow that deploys it. The public repo ships the image and a sample config. This repo is the only place the chat ID, the repo list and the site list live.

## Layout

```
mercury.yaml                 the config, copied from the public sample and filled in
.github/workflows/deploy.yml runs the public Bicep at the pinned commit with this config
```

## Rules

- `PUBLIC_SHA` in `deploy.yml` pins one public commit, and both the Bicep and the image come from it. It is bumped by hand, never to a branch or `latest`. A public commit must not change what talks to the phone before it has been read.
- This workflow is the only deploy. The public repo builds and publishes an image on every push to `main` and deploys nothing.
- Secrets are Actions secrets here and Container Apps secrets in Azure. Nothing secret is in `mercury.yaml`.
- Every push to `main` deploys. There is no staging, because the config is small enough to read before pushing.

## Secrets this repo's Actions need

`AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and `AZURE_SUBSCRIPTION_ID`, the same OIDC app registration the public repo signs in as. `DATABASE_URL`, `MODEL_API_KEY` and `MERCURY_BEARER_TOKEN` go to the Bicep, and the workflow refuses to deploy if any of the three is missing, because the Bicep would otherwise remove it from the live apps. `VOYAGE_API_KEY` is optional. `PAGESPEED_API_KEY` is optional to the deploy, but without it the cloud Lighthouse fallback sends keyless requests, whose shared daily quota was already spent on 2026-09-24. The variables `MODEL` and `MODEL_BASE_URL` pick the provider. Step 3 adds `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `MERCURY_GITHUB_TOKEN` and `ANTHROPIC_API_KEY`.
