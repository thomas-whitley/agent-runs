# n8n: a labelled issue becomes a repo chore

One n8n workflow, `issue-to-chore.json`, runs on the home PC beside the checks worker. It turns an open issue labelled `mercury` on `thomas-whitley/mercury-fixture` into a `repo_chore`, and reports the result back on the issue. Nothing of it runs in Azure, and nothing in CI tests it.

## What it does

Every 15 minutes, on the quarter hour, the workflow runs two branches.

The first lists the fixture's open issues labelled `mercury`. Each one it has not seen before is posted to `POST /runs` as a `repo_chore` on the fixture, with the issue's title and body as the instruction and `source: n8n`. Mercury creates the run `awaiting_approval` and asks the owner on Telegram, the same gate as a chore from chat (`app/chores.py`). The workflow keeps a map of issue number to run id in its static data, so an issue whose chore is still waiting is not posted again.

The second reads each tracked run from `GET /runs/{id}`. A run has finished once `duration_seconds` is set. For each finished run it reads the run's events, takes the `done` event's output, comments on the issue with the pull request link or the status and reason, removes the `mercury` label and stops tracking the issue. A run the owner declined, or one whose question expired, ends `cancelled` and is reported the same way.

It polls GitHub because the home PC has no public address, and a webhook would need a tunnel, which is one more thing to keep running.

## Running it

```
docker compose -f n8n/compose.yml up -d
```

The editor is at http://localhost:5678, bound to this machine only. The workflow needs two credentials of type Bearer Auth, which live in n8n's own encrypted store in the `n8n_data` volume and never in this repo.

- `Mercury bearer token` (id `mercuryBearer01`), the API's `MERCURY_BEARER_TOKEN`.
- `GitHub mercury-fixture` (id `githubFixture01`), a fine grained token for `mercury-fixture` only, with Issues read and write.

Create both in the editor under those names, or import them with the CLI from a file inside the container that is deleted straight after, which is how they were set up here. Then import and publish the workflow, and restart n8n, because a workflow published from the CLI only takes effect on the next start:

```
docker cp n8n/issue-to-chore.json n8n-n8n-1:/tmp/wf.json
docker exec n8n-n8n-1 n8n import:workflow --input=/tmp/wf.json
docker exec n8n-n8n-1 n8n publish:workflow --id=mercuryIssueChore
docker restart n8n-n8n-1
```

The export names its credentials by id and carries no secret. Check that again before committing a new export.

## Where n8n helped and where it did not

n8n earned its place on the parts that are plumbing. The 15 minute schedule, the credential store, the HTTP calls to GitHub and to Mercury, and a record of every execution came with it, and none of it needed a line in the API or a new Azure resource. The chore itself, the approval and the pull request stayed in Mercury, so n8n only had to make five HTTP calls in the right order. It did not help with anything that needed state or parsing. Remembering which issues were already posted, matching each run back to its issue and reading the `done` event out of the SSE text all ended up in four Code nodes of JavaScript, and in n8n 2.x a Code node that returns new items has to set `pairedItem` by hand, or the next HTTP node cannot find the issue number it came from. Static data persists only in executions the schedule starts, not in a manual test run, so the dedupe cannot be tried from the editor without posting the same issue twice. The workflow has no automated test. It was written as JSON by a short script rather than in the editor, and checked by running it against the live API. Written in Python inside the scheduler Job it would have been about the same length, tested in CI, and one less container on the home PC. What n8n bought was not having to touch the deployed service at all.

## The first run

Issue [#3](https://github.com/thomas-whitley/mercury-fixture/issues/3), "Add multiply to calc.py, with a test", was opened and labelled at 08:20:06 UTC on 2026-10-02. The workflow picked it up on its 08:30 tick and created run `100c1125-a731-4a16-a3a3-3edfa1e8aad7` with `source: n8n` at 08:30:49, and the approval question was sent to the owner's chat as message 37.

Telegram's apps could not connect that evening, on the phone over Wi-Fi and mobile data and on the web client, so the button press never left the phone. At 09:46 the owner sent the same press to the webhook from a shell, with the webhook secret, which is the request Telegram itself would have made. The worker took the run at once, and it opened [pull request #4](https://github.com/thomas-whitley/mercury-fixture/pull/4) at 09:47:00 (10 lines added in `calc.py` and `test_calc.py`, 597 tokens).
