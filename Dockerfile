# The runs page. Built here so the Python stage never needs Node.
FROM node:22-slim AS web

WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /usr/local/bin/uv

WORKDIR /srv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/srv/.venv

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY app ./app
COPY migrations ./migrations
COPY corpus ./corpus
COPY --from=web /web/dist ./web/dist
RUN uv sync --locked --no-dev

ENV PATH="/srv/.venv/bin:$PATH"

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
