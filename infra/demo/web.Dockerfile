# CourseFlow web frontend for the hosted demo. Build context: the repository
# root (see compose.yml). NEXT_PUBLIC_API_BASE_URL is compiled into the
# browser bundle at build time: it is public, and the URL the BROWSER uses
# to reach the API (not a container-internal address).

FROM node:22.12.0-bookworm-slim AS build

ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
ARG NEXT_PUBLIC_API_BASE_URL
ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL}
RUN npm run build

FROM node:22.12.0-bookworm-slim AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1
WORKDIR /web
COPY --from=build --chown=node:node /web /web
USER node
EXPOSE 3000
CMD ["node_modules/.bin/next", "start", "--hostname", "0.0.0.0", "--port", "3000"]
