FROM python:3.13-alpine
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN addgroup -S app && adduser -S app -G app
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
COPY --chown=app:app . .
RUN mkdir -p /app/staticfiles && chown app:app /app/staticfiles
USER app