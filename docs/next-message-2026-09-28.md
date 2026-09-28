# Next session: steps 6a and 6b before the rest of 2d

Paste this as the first message on the other laptop. Delete this file in the session's first commit, because the build brief is the plan and this note only changes its order.

Pull first. Main is at e88d0de plus the commit that added this file.

## The change of order, decided by Thomas on 2026-09-28

Thomas needs the runs page on screen for a recording on Wednesday 30 September, so steps 6a and 6b come next, ahead of the rest of 2d and ahead of steps 3 to 5. Stop 2d where it stands. The claim window and the PageSpeed fallback are in (8bc9e4f to e88d0de). The crawl, `broken_links` in `KINDS`, `checks/compose.yml`, the Lighthouse integration test in CI and README claim seven all wait. After 6b, go back to 2d and then follow the brief's order unchanged. Write the reorder into `docs/handoff.md` so the next session does not take 6c as next.

Aim to have 6b live by Wednesday evening, Melbourne time. If only 6a lands, stop there and say so in the handoff. A half-built 6b does not go live.

## 6a. The pipeline, with a near empty page

This is the brief's 6a, with these notes from the current code.

- `web/` is Vite, React and TypeScript with its own `package.json`, on Node 22. Pin npm through `packageManager` as `checks/` does, using corepack on this machine.
- The Dockerfile becomes multi stage. A Node stage builds `web/`, and the Python stage copies `web/dist` in next to `app`. `COPY static ./static` goes, along with `static/index.html` and the `demo_page` route in `app/main.py`. Task 7's page was always marked throwaway, and this is the step that replaces it.
- FastAPI serves the bundle, not nginx. Every API route is registered before the page, so `/health`, `/runs`, `/runs/{id}/events` and `/checks/*` never reach the page. Any unmatched GET falls through to `index.html`. Write that test first. It should assert that `GET /runs` still returns JSON, and that an unknown path such as `/runs-page` returns the HTML.
- `ci.yml` gains a `web` job running typecheck and Vitest, next to the existing `checks` job. `deploy.yml` publishes the image, so it gets the same job ahead of the build.
- Deploys now run from the private `mercury-config` repo on a pinned image tag. The exit condition is a page loading from the deployed image, so it needs that pin bumped to the new tag. Tell Thomas the tag. He bumps it himself.

*Exit: a page saying nothing but its own title loads from the live URL, and both workflows are green.*

## 6b. The runs list

This is the brief's 6b. The list reads `GET /runs` newest first and shows type, provider, executor, status, tokens and duration. It follows the `next_cursor` keyset pagination the endpoint already serves. Metadata only: no event bodies, no token in the browser, and no login. `GET /runs` is already public and stays that way.

- Check that `serialize_run_row` carries everything the list shows. If duration is not there, compute it in the browser from the two timestamps rather than changing the API's shape.
- The Vitest test covers the pagination boundary. A page of exactly `limit` rows shows a next control, a shorter page shows none, an empty first page shows an empty state, and a second page never repeats the last row of the first.
- The hourly uptime runs will make up most of the live list. That is the real data, so leave it. Do not seed runs to make the page look busier. Before the recording, one real public `pytest` run and one Lighthouse check claimed by the laptop worker will give the list some variety.

*Exit: the list renders real runs from the live deploy, and the Vitest test passes.*

## Rules that still hold

- Decision 43 holds. No README line, CV line or other text claims React until 6b's exit is met on the live deploy. After that, the README gains one sentence under the design section saying what the page shows. Claim six stays as it is until 6c.
- The writing rules in `CLAUDE.md` apply to the README, the docs and the commit messages.
- Nothing from Thomas's employer appears anywhere.
- No paid resource. The page ships inside the existing API container.

## When done

Push, update `docs/handoff.md` with the live URL, the image tag and what the list shows, and tell Thomas the page is live so he can record.
