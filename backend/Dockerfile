FROM python:3.13-alpine
# Pinned, not :latest: must match the uv that writes uv.lock on the host, so host and
# containers interpret the lockfile identically. Bump together with the host's uv.
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv
RUN addgroup -S app && adduser -S app -G app
WORKDIR /app
COPY pyproject.toml uv.lock ./
# --locked (not --frozen): refuse to build if uv.lock is stale relative to pyproject.toml.
RUN uv sync --locked --no-dev
ENV PATH="/app/.venv/bin:$PATH"
COPY --chown=app:app . .
RUN mkdir -p /app/staticfiles && chown app:app /app/staticfiles
USER app