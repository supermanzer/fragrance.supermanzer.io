# fragrances.supermanzer.io

A personal fragrance recommendation web app. Each month it analyzes your collection, searches the web for new releases, and emails you a curated shortlist of picks matched to your taste profile.

Rebuilt from a previous agent running on a MicroK8s / Raspberry Pi cluster — same output, stack you control.

---

## Stack

| Layer | Technology |
|---|---|
| Frontend | Nuxt 4 (Vue 3, SSR, `app/` directory) + Vuetify 3 |
| Backend | Django 6 + Django REST Framework |
| Task queue | Celery + `django-celery-beat` (DB-stored schedules) |
| Message broker | Redis 7 |
| Database | PostgreSQL 16 |
| Web search | SearXNG (self-hosted, internal Docker network only) |
| LLM | Anthropic SDK (4 structured calls per run) |
| Deployment | Docker Compose on a single VPS behind nginx |

---

## Features

- **Collection management** — add, edit, and delete fragrances with status (`own`, `like`, `dislike`) and notes
- **CSV import** — upload a spreadsheet export to bulk-add or update your collection; tolerates varied column casing and status labels (`Don't Like` → `dislike`, etc.)
- **Recommendation runs** — trigger a full AI-powered recommendation pipeline on demand or on a monthly schedule
- **Preference profile** — automatically generated taste analysis (loved/liked/disliked notes, search angles) used to guide recommendations
- **Email delivery** — rendered HTML email sent via Gmail SMTP with your monthly picks
- **Settings** — configure Gmail credentials, send schedule, and re-import your collection at any time

---

## Project layout

```
fragrances.supermanzer.io/
  backend/          Django project + Celery tasks
  frontend/         Nuxt 4 app
  nginx/            nginx.conf
  searxng/          SearXNG config (mounted into container)
  docker-compose.yaml
  docker-compose.override.yaml   # dev overrides (gitignored in prod)
```

---

## Recommendation pipeline

One Celery task, six steps:

```
monthly_fragrance_run(user_id)
  1. generate_preference_profile    LLM — reads Fragrance table → writes PreferenceProfile
  2. run_discovery_searches          searches SearXNG × 2 angles
  3. select_candidates               LLM — picks 5 from search results
  4. verify_candidates               5 parallel searches + LLM verification
  5. generate_email_content          LLM — rationale per pick + personalized intro
  6. render_and_send_email           Django template → HTML; Gmail SMTP
```

---

## Local development

### Prerequisites

- Docker Desktop
- `docker compose` v2

### 1. Clone and configure environment

```bash
git clone git@github.com:supermanzer/fragrance.supermanzer.io.git
cd fragrance.supermanzer.io
cp backend/.env.example backend/.env   # then fill in values
```

Required variables in `backend/.env`:

```env
SECRET_KEY=your-django-secret-key
DEBUG=True
DB_PASSWORD=localdevpassword
ANTHROPIC_API_KEY=sk-ant-...
CORS_ALLOWED_ORIGINS=http://localhost:3000,http://localhost:3434
```

### 2. Start the stack

```bash
docker compose up
```

The override file (`docker-compose.override.yaml`) is merged automatically. It:
- Exposes Django at `http://localhost:8000` and Nuxt at `http://localhost:3434`
- Bind-mounts backend source for live reload
- Runs `npm install && npm run dev` in the frontend container

### 3. Run migrations and create a superuser

```bash
docker compose exec backend uv run python manage.py migrate
docker compose exec backend uv run python manage.py createsuperuser
```

### 4. Import an existing collection (optional)

```bash
docker compose exec backend uv run python manage.py import_fragrance_collection \
  --user-id=1 --csv=/path/to/fragrances.csv
```

CSV must have columns: `fragrance`, `status`, `house`, `notes`.
Column names are case-insensitive. Status values accept: `own`, `like`, `dislike`, `Don't Like`.

---

## Production deployment

Production uses the base `docker-compose.yaml` only (no override). nginx terminates TLS and proxies to backend and frontend over the internal Docker network. SearXNG has no external port — only the backend can reach it.

Set `PUBLIC_API_BASE` in the environment or a production `.env`:

```env
PUBLIC_API_BASE=https://fragrances.supermanzer.io/api/v1
```

---

## Key domain models

| Model | Purpose |
|---|---|
| `FragranceConfig` | Gmail credentials + run schedule (OneToOne with User) |
| `Fragrance` | User's collection; status ∈ {own, like, dislike} |
| `PreferenceProfile` | LLM-generated taste analysis, regenerated each run |
| `RecommendationRun` | One pipeline execution with status, email HTML, timestamps |
| `Recommendation` | One pick per run; permanent dedup guard for future runs |
