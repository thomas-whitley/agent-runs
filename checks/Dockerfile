# The self hosted checks worker: Node 22 and Debian's Chromium, run by
# checks/compose.yml on the machine that owns Lighthouse and the crawl.
FROM node:22-bookworm-slim AS build

WORKDIR /checks
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json ./
COPY src ./src
RUN npm run build

FROM node:22-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium fonts-liberation ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /checks
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY --from=build /checks/dist ./dist

# chrome-launcher reads CHROME_PATH. The sandbox flag is explained in
# src/chrome-flags.ts; /dev/shm is 64 MB in a container, too small for Chrome.
ENV CHROME_PATH=/usr/bin/chromium \
    CHROME_EXTRA_FLAGS="--no-sandbox --disable-dev-shm-usage"

USER node
CMD ["node", "dist/main.js"]
