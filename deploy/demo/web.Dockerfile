# The web half of the demo deploy — see docs/DEPLOY.md for the full runbook.
#
# This serves `web/` as a Next.js NODE server (`next build` + `next start`), built with
# `NEXT_PUBLIC_API_MODE=demo`: the root page (`/`) is `components/DemoApp.tsx`, a real visitor
# session against `POST /v2/demo/session` and its siblings (see `web/README.md`, "Try it live —
# the public demo"). The sample workspace (no model) stays reachable at `/tour`. `lib/demo-api.ts`
# reads `NEXT_PUBLIC_API_BASE_URL`, left unset here so every `/v2/demo/*` call is same-origin —
# Caddy (see `Caddyfile`) routes `/v2/*` on that same origin to the `api` container, so no CORS
# configuration is needed.
#
# A STATIC export cannot serve this build: `next.config.mjs`'s own `assertStaticExportIsSample`
# guard refuses `PM_STATIC_EXPORT=1` with anything but `NEXT_PUBLIC_API_MODE=sample`, because a
# static export has no server behind it to reach `/v2/demo/*` at all (see that guard's own
# comment). `PM_STATIC_EXPORT` is therefore never set in this file.
#
# Build context is the repository root; `web/` is copied in explicitly.

FROM node:20-slim AS build
WORKDIR /app
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
# Font assets are committed to git (`web/public/fonts/`, `git ls-files` confirms), so no
# `npm run fonts` network fetch is needed at build time.
ENV NEXT_PUBLIC_API_MODE=demo
RUN npm run build

FROM node:20-slim
WORKDIR /app
# Only what `next start` needs at runtime: the build output, the public assets, the compiled
# dependency tree, and the two config files `next start` loads (`next.config.mjs` itself imports
# `scripts/build-guards.mjs`, so that file must travel with it).
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/.next ./.next
COPY --from=build /app/public ./public
COPY --from=build /app/next.config.mjs ./next.config.mjs
COPY --from=build /app/scripts ./scripts
COPY --from=build /app/package.json ./package.json
ENV NEXT_PUBLIC_API_MODE=demo
# `node` is the image's own built-in non-root user (uid 1000, created by the upstream Dockerfile).
USER node
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD node -e "require('http').get('http://127.0.0.1:3000/',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"
CMD ["npm", "start"]
