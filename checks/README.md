# checks

The self hosted checks worker from `docs/mercury.md`. It claims Lighthouse checks from the API, runs each one in headless Chrome on this machine, and posts back a summary of about 300 bytes: the five category scores, largest contentful paint, total blocking time and the ids of the audits that scored under 0.9. The full report is discarded. When a check fails, the result carries the error and the worker's last 50 log lines instead.

It talks to the API over HTTPS with the bearer token and never holds a database credential, so the token is the only secret on this machine. That token can only claim, heartbeat and close `site_check` runs of the kinds this worker declares.

## Running it

Node 22.19 or later and Google Chrome are required.

```
npm ci
npm run build
API_BASE_URL=https://<api host> MERCURY_BEARER_TOKEN=<token> npm start
```

`WORKER_ID` defaults to the hostname and `POLL_SECONDS` to 60. `node dist/main.js --once` polls a single time and exits. While a check runs, the worker heartbeats every 30 seconds against the API's two minute lease. SIGINT or SIGTERM ends an idle wait at once and lets a running check finish.

## Tests

```
npm run typecheck
npm test
```

The tests fake the API with a recording `fetch` and stub the Lighthouse call, so they need no Chrome and no network.
