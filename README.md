# Niche Podcast Clipper

> Turn long-form podcast episodes into viral, bite-sized vertical clips for TikTok, Instagram Reels, and YouTube Shorts using locally hosted, lightweight open-source **Hugging Face Transformers** models for entirely on-premise speech-to-text and transcript analysis.

## 🚀 Overview

An all-in-one platform for podcasters, agencies, and creators. Lightweight HF models (`openai/whisper-base`, `google/flan-t5-base`) run inside a FastAPI backend, secured with JWT, queued via Redis/Celery, and cut with pure Python OpenCV — no FFmpeg binaries, no cloud storage, no API fees.

### Feature highlights

- **Local ASR + highlight AI.** Whisper transcribes with word-level timestamps; Flan-T5 scores
  candidate windows for virality, hook strength, emotion and quotability — fully on-premise.
- **OpenCV vertical rendering.** Word-by-word caption burning, thumbnails, caption-free preview
  companion renders, H.264 muxing with audio restore, and an audio-only gradient path.
- **Clip editor workflow.** Draft editing with live caption overlay, presets, PATCH-based
  invalidation of stale renders.
- **Favourites.** Star the best highlights and filter the library by `favorite=true`.
- **Public share links.** Tokenised, optionally expiring, revocable URLs that expose clip
  metadata, thumbnail and MP4 download without an account (view-counted).
- **SRT / WebVTT subtitle export** generated from the same word-timing pipeline as the burned
  captions.
- **Global search** across episodes and clips in one request.
- **Dashboard analytics** (`/stats/overview`): per-status totals, storage usage, top clips,
  7-day render activity.
- **Batch rendering** and **CSV export** of the whole clip library.
- **Admin user management** (superuser-only activation/deactivation and superuser grants).
- **Production hardening.** Strict auth rate limiting, per-object ownership checks, production
  error masking, DB pool recycling, non-root Docker image with healthchecks, `.dockerignore`
  and `.env.example` shipped in-repo.

## 🛠 Tech Stack

- **Frontend:** React (Vite), raw vanilla CSS, React Context API
- **Backend:** FastAPI, Uvicorn, Celery
- **AI:** Hugging Face Transformers (local PyTorch pipelines)
- **State/Cache:** Redis (broker, result backend, token cache, rate limits)
- **Security:** JWT (python-jose, OAuth2 Password Bearer)
- **Video:** OpenCV (frame-level crop, zoom, caption burning)
- **Data:** PostgreSQL (SQLAlchemy + Alembic), local `./media_storage`

## 📦 Getting Started

### Prerequisites

Python 3.10+, Node.js 18+, PostgreSQL and Redis running locally.

### Backend

```bash
cd backend
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env       # then edit DATABASE_URL / SECRET_KEY
nano .env                  # see "Configuration" below
alembic upgrade head       # optional — the app also applies migrations at startup
uvicorn main:app --reload --port 8000
```

#### Configuration (`.env`)

A ready-made template ships as **`backend/.env.example`** — copy it to `backend/.env` and fill
in the secrets. The app still runs on sensible defaults when the file is absent. At minimum set:

```dotenv
ENVIRONMENT=development          # or "production"
DEBUG=false
FRONTEND_ORIGINS="http://localhost:5173,http://127.0.0.1:5173"
DATABASE_URL="postgresql://postgres:postgres@localhost:5432/podcast_clipper"
SECRET_KEY="<openssl rand -hex 32>"
REFRESH_SECRET_KEY="<openssl rand -hex 32>"
```

> In production, template/weak `SECRET_KEY` values are rejected at startup.

> **Model cache tip.** `HF_HOME` defaults to `./models`. Fresh deployments are encouraged to set
> `HF_HOME=./model_cache` so the Hugging Face cache lives *outside* the Python `models` package
> directory. If you already downloaded checkpoints under the old default, move them once:
> `mkdir -p model_cache && mv models/hub model_cache/hub`.

Interactive API docs: <http://localhost:8000/docs>

### Troubleshooting first run

| Error | Fix |
| --- | --- |
| `SettingsError: error parsing value for field "FRONTEND_ORIGINS"` | Older `pydantic-settings` tried to JSON-decode the CSV list — already fixed in `core/config.py` (uses `NoDecode` + CSV validator). Run `pip install -r requirements.txt` to sync versions. |
| `password authentication failed for user "postgres"` | The role password doesn't match `DATABASE_URL`. One-time setup: `sudo -u postgres psql -f scripts/db_setup.sql` (sets password `postgres` and creates the `podcast_clipper` database), or edit `DATABASE_URL` in `backend/.env` to match your existing role. |
| `connection refused ... port 5432` | Start the database: `sudo systemctl start postgresql` |
| `Redis unavailable` warning at startup | Start Redis: `sudo systemctl start redis-server` (caching/queues degrade but the API still boots). |
| `429 Too many authentication attempts` | The strict auth limiter kicked in (default 10 login/register/refresh attempts per IP per minute). Wait for the window or raise `AUTH_RATE_LIMIT_REQUESTS`. |

### Celery worker (background AI inference + rendering)

```bash
cd backend
./venv/bin/celery -A worker.celery_app worker --loglevel=info
```

> **Celery 5.4 has no `--autoreload` flag** (`celery worker --help` lists only
> `--autoscale`, `--purge`, `--max-tasks-per-child`, …). `--autoreload` is a
> *Django*-Celery feature, not a Celery core one, and passing it fails with:
>
> ```
> Error: No such option '--autoreload'. Did you mean '--autoscale'?
> ```
>
> Celery imports the task modules **once at startup** and keeps the compiled
> code in memory, so editing `services/ai/highlights.py` (or any task module) has
> **no effect** on a running worker — it keeps executing the code it loaded.
> The tell-tale symptom is a traceback pointing at a line that no longer exists
> in the file. Either **restart the worker** after a code change, or wrap it
> with the `watchfiles` runner that is already installed (see below).

#### Auto-restart on code changes (optional, development only)

`watchfiles` is already in `requirements.txt`, so no extra install is needed:

```bash
cd backend
./venv/bin/watchfiles --filter python \
  './venv/bin/celery -A worker.celery_app worker --loglevel=info' \
  services tasks core
```

It watches `services/`, `tasks/` and `core/` and restarts the worker whenever a
`.py` file changes (verified: it prints `1 change detected` and re-boots the
worker). Not recommended in production — every edit drops in-flight tasks'
worker process.

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173>.

## 💡 Workflow

1. **Authenticate** — sign in via the React app to obtain a JWT session.
2. **Upload** — drop an MP3/WAV/MP4 into the dashboard; it lands in `./media_storage`.
3. **Local AI** — Whisper transcribes with timestamps; Flan-T5 surfaces the most viral windows asynchronously via Celery + Redis.
4. **Review & edit** — browse clips, tweak caption styles (TikTok/Hormozi/minimal), adjust framing coordinates with a live preview.
5. **Save & render** — edit the transcript and re-render the clip with matching captions burned in, then download the MP4.
6. **Export** — download the rendered MP4 directly from local storage.

---

# 🧠 Architecture

## High-level request flow

```
┌──────────────────┐   JWT / axios   ┌────────────────────┐
│  React SPA       │ ───────────────▶│  FastAPI (main.py) │
│  Vite :5173      │◀─────────────── │  Uvicorn :8000     │
└──────────────────┘   JSON + media  └─────────┬──────────┘
                                               │
        ┌──────────────────────────────────────┼───────────────────────────┐
        │                                      │                           │
        ▼                                      ▼                           ▼
 ┌─────────────┐  enqueue task      ┌──────────────────┐        ┌──────────────────┐
 │ PostgreSQL  │  (Redis broker)    │  Celery worker   │        │  HF model cache  │
 │ SQLAlchemy  │◀──────────────────▶│  ai_tasks /      │───────▶│  ./models (local)│
 │ + Alembic   │  job mirror rows   │  video_tasks     │        │  Whisper, T5     │
 └─────────────┘                    └────────┬─────────┘        └──────────────────┘
                                             │ OpenCV frame
                                             ▼  loop
                                    ┌──────────────────┐
                                    │ ./media_storage  │
                                    │ uploads + MP4s   │
                                    └──────────────────┘
```

**Design principles**

- **Everything local.** ASR (Whisper) and highlight analysis (Flan-T5) both run on-premise; no transcript or audio ever leaves the machine.
- **Synchronous API, asynchronous work.** Long operations (transcription, highlight extraction, rendering) are always returned as a job handle and executed by Celery.
- **Job mirroring.** Every Celery task has a `render_jobs` row so progress is queryable via SQL, independent of the Redis result backend.
- **No FFmpeg dependency for editing.** Video work is pure `opencv-python-headless`. The bundled `imageio-ffmpeg` binary is used only to mux/transcode audio into browser-playable H.264.

---

# 🔧 Backend Documentation

## Directory layout

```
backend/
├── main.py                     FastAPI entry point: CORS, security headers, lifespan, /media mount
├── worker.py                   Celery worker entry point (registers tasks + beat schedule)
├── alembic/                    Database migrations (0001…0003)
├── alembic.ini
├── requirements.txt
├── .env.example                Configuration template — copy to `.env`
├── Dockerfile                  python:3.11-slim, non-root user, healthcheck on /ping
├── .env                        Runtime configuration (see Configuration matrix)
│
├── api/
│   ├── deps.py                 get_current_user, get_current_superuser, _load_user,
│   │                           rate_limit (API) + rate_limit_auth (credential endpoints)
│   └── v1/
│       ├── router.py           Aggregates health, auth, episodes, clips, jobs,
│       │                       search, stats, public, admin routers
│       └── routers/
│           ├── health.py       /health (+ disk headroom), /health/models, /health/worker
│           ├── auth.py         register, token, login, refresh, logout, me  (strict rate limit)
│           ├── episodes.py     upload, list, detail, patch, delete, transcribe, transcript, media, stats
│           ├── clips.py        generate, list, presets, export, render-batch, detail, patch, delete,
│           │                   render, favorite, shares, captions-file, preview, download
│           ├── jobs.py         list, detail, revoke, worker/ping
│           ├── search.py       GET /search across episodes + clips
│           ├── stats.py        GET /stats/overview dashboard analytics
│           ├── public.py       Tokenised share links (no auth): metadata/download/thumbnail
│           └── admin.py        Superuser account management
│
├── core/
│   ├── config.py               Pydantic Settings (typed, .env-backed, production secret guard)
│   ├── security.py             Password hashing + JWT encode/decode, token types
│   ├── celery_app.py           Celery instance, time limits, queue config
│   ├── redis_client.py         Cache get/set, prefix invalidation, fixed-window rate limiter
│   └── logging_config.py       Structured logging setup
│
├── db/
│   ├── session.py              Engine (pool_pre_ping + pool_recycle), SessionLocal, Base, get_db
│   └── init_db.py              Schema bootstrap helper
│
├── models/                     SQLAlchemy ORM: user, episode, transcript, clip, clip_shares, enums
├── schemas/                    Pydantic contracts: auth, episode, clip, common, search, admin
├── services/
│   ├── storage.py              Local FS layout, relative/absolute path helpers
│   ├── media_probe.py          OpenCV probing: duration, resolution, fps, stream flags
│   ├── caption_styles.py       Caption presets + style/framing coercion helpers
│   ├── subtitles.py            SRT / WebVTT export built on the caption word timings
│   ├── ai/
│   │   ├── model_registry.py   Lazy thread-safe singletons for ASR + seq2seq
│   │   ├── transcription.py    Whisper wrapper producing word/segment timings
│   │   ├── highlights.py       Candidate windowing, heuristics, T5 scoring, selection
│   │   └── audio_utils.py      Decoding/resampling helpers
│   └── video/
│       ├── renderer.py         Frame loop, H.264 muxing, audio restore, thumbnails, preview_frame
│       ├── captions.py         Word timing resolution, grouping, OpenCV caption burning
│       └── framing.py          Face detection + focus-point tracking
└── tasks/
    ├── ai_tasks.py             transcribe_episode_task, extract_highlights_task, prewarm_models_task
    └── video_tasks.py          render_clip_task
```

## API reference

All application routes are prefixed with `settings.API_V1_PREFIX` (default `/api/v1`).
Except for `/auth/register`, `/auth/token`, `/auth/login` and `/auth/refresh`, every endpoint
requires an `Authorization: Bearer <access_token>` header. Media download routes additionally
accept a `?token=` query parameter so plain `<video>`/`<img>` tags (which cannot set headers)
keep working.

### Root & utility (no prefix)

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| `GET` | `/` | — | Service banner: name, version, environment, storage root, active AI models, docs links. |
| `GET` | `/ping` | — | Liveness probe that never touches the database. Used by the Docker `HEALTHCHECK`. |
| `GET` | `/ready` | — | Readiness probe that verifies PostgreSQL and Redis before traffic. Returns `200` only when both dependencies answer, otherwise `503` with a per-dependency `checks` blob (`database` / `redis` booleans plus `version`). Distinct from `/ping`, which only reports that the process is up. |
| `GET` | `/docs`, `/redoc` | — | Swagger UI / ReDoc. Disabled outside development via `ENABLE_DOCS=false`. |
| `GET` | `/media/*` | — | Static mount over `media_storage`, **development only**; disabled in production. |

### Health

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | `ok` when both PostgreSQL and Redis answer, otherwise `degraded`, with per-dependency booleans. |
| `GET` | `/health/models` | Configured ASR/highlight checkpoints, device, chunk length, offline mode. Never blocks (models load lazily). |
| `GET` | `/health/worker` | Pings Celery workers over the broker; reports `workers_online` / `healthy`. |

### Authentication

All credential endpoints (`register`, `token`, `login`, `refresh`, `logout`) are protected by the
**strict auth rate limiter** (`AUTH_RATE_LIMIT_REQUESTS`, default 10 requests per IP per minute)
to blunt password-guessing; the general API budget still applies everywhere else.

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/auth/register` | Create an account (email, password, optional `full_name`). `409` on duplicate email. |
| `POST` | `/auth/token` | OAuth2 password flow (`x-www-form-urlencoded`; `username` = email). Returns access + refresh JWTs. |
| `POST` | `/auth/login` | JSON variant used by the SPA. |
| `POST` | `/auth/refresh` | Exchange a valid refresh token for a fresh token pair. |
| `POST` | `/auth/logout` | Blacklist the presented refresh token (by `jti` in Redis) and drop the cached session. Idempotent — an already-invalid token is not an error. |
| `GET` | `/auth/me` | Current user profile. |
| `PATCH` | `/auth/me` | Update `full_name` and/or `password` (invalidates the cached user entry). |
| `GET` | `/auth/session` | Session introspection: whether a Redis session exists for the caller plus the configured access/refresh TTLs. Hidden from the OpenAPI schema (`include_in_schema=False`). |

**Token model.** Access tokens are short-lived (`ACCESS_TOKEN_EXPIRE_MINUTES`, default 30),
refresh tokens long-lived (`REFRESH_TOKEN_EXPIRE_MINUTES`, default 7 days). Each carries a
`type` claim (`access` / `refresh`) validated by `core/security.decode_token`, so a refresh
token cannot be replayed as an access token.

**Sessions, refresh rotation and revocation.** A successful login records `session:{user_id}` in
Redis. `POST /auth/refresh` **rotates** the refresh token and blacklists the old one by writing
`revoked:{jti}` (retained for `BLACKLIST_TTL`), then returns a fresh pair — so a leaked refresh
token is usable at most once. `POST /auth/logout` applies the same blacklist entry and deletes
the session key.

### Episodes

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/episodes/upload` | Multipart upload (`file`, optional `title`, `description`, `auto_transcribe`). Streams to disk in 1 MB chunks, enforces `MAX_UPLOAD_SIZE_MB` (`413` when exceeded), rejects unsupported extensions (`415`), probes the media with OpenCV, then moves it into the per-episode directory. |
| `GET` | `/episodes` | Paginated list (`skip`, `limit`, `status`) ordered newest first, eagerly loading transcripts. |
| `GET` | `/episodes/{id}` | Episode detail. |
| `PATCH` | `/episodes/{id}` | Update `title` / `description`. |
| `DELETE` | `/episodes/{id}` | Delete the episode, its derived artifacts and its highlight cache entry. |
| `POST` | `/episodes/{id}/transcribe` | Queue the local ASR pipeline. Body: `language`, `word_timestamps`, `force`. Without `force`, an existing transcript is returned with `cached: true`. |
| `GET` | `/episodes/{id}/transcript` | Timestamped transcript with segments and word-level timings. `404` when not yet transcribed. |
| `GET` | `/episodes/{id}/media` | Streams the source media (supports range requests). Accepts `?token=`. |
| `GET` | `/episodes/{id}/stats` | Dashboard counters: duration, `has_transcript`, clip count, rendered count, best virality score, total output bytes. |

### Clips

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/clips/generate?episode_id=` | Runs the local highlight extractor and persists clips. Body: `clip_count`, `min_duration_seconds`, `max_duration_seconds`, `temperature`, `focus`, `use_cache`, `caption_style`, `framing`. Results are cached in Redis under `highlights:`. `409` if the episode has no transcript. |
| `GET` | `/clips` | List clips ordered by virality score. Filters: `episode_id`, `min_score`, `skip`, `limit`, `favorite` (boolean), `search` (case-insensitive match on title/hook/summary/transcript). |
| `GET` | `/clips/presets` | Built-in caption themes with labels and full style payloads. |
| `GET` | `/clips/export` | Download the whole clip library (optionally `?episode_id=`) as an RFC 4180 CSV with scores, status and favourite flags. |
| `POST` | `/clips/render-batch` | Queue renders for up to 50 clips in one call (`{"clip_ids": [...], "priority": 0-9}`). Duplicates collapse; clips that are missing/already rendering are reported in `skipped` instead of failing the batch. Returns `queued` + `skipped`. |
| `GET` | `/clips/{id}` | Clip detail including caption style and framing configuration. |
| `PATCH` | `/clips/{id}` | Update `title`, `hook`, `summary`, `transcript_text`, `start_time`, `end_time`, `caption_style`, `framing`. Validates `end_time > start_time`, recomputes duration, **invalidates the previously rendered file** (`output_path` cleared, status back to `ready`) and clears related cache keys. |
| `DELETE` | `/clips/{id}` | Delete the clip and its whole storage directory. |
| `POST` | `/clips/{id}/render` | Queue caption burning + framing. Body: `caption_style`, `framing`, `transcript`, `priority` (0–9). The `transcript` (when sent) is persisted so the burned captions match the stored text. Returns `clip_id`, `job_id`, `task_id`. |
| `GET` | `/clips/{id}/status` | Latest `RenderJob` for polling. |
| `GET` | `/clips/{id}/download` | Download the rendered MP4. Accepts `?token=`. |
| `GET` | `/clips/{id}/thumbnail` | Download the generated JPEG thumbnail. Accepts `?token=`. |
| `GET` | `/clips/{id}/preview` | Renders a **single** composited JPEG frame (no full encode) for the editor. Query params: `timestamp` (clip-relative), `caption_style` (JSON), `framing` (JSON) for unsaved draft values. Sets `Cache-Control: no-store`. |
| `GET` | `/clips/{id}/caption-timeline` | Every caption group for the clip in one JSON payload with **clip-relative** timings, so the editor composites the overlay locally instead of requesting a frame per playback position. Query params: `caption_style` (JSON), `transcript`, `start_time`, `end_time` for unsaved draft values. |
| `GET` | `/clips/{id}/jobs` | Render history for the clip (last 20). |
| `POST` | `/clips/{id}/favorite` | Toggle the star flag; returns the updated clip. |
| `POST` | `/clips/{id}/shares` | Mint a public share link (body optional: `expires_in_days` 1-365). Returns `url`, `token`, `is_active`. |
| `GET` | `/clips/{id}/shares` | List the clip's share links with view counts. |
| `DELETE` | `/clips/{id}/shares/{share_id}` | Revoke a link; its token stops resolving immediately (`410` afterwards). |
| `GET` | `/clips/{id}/captions-file?fmt=srt\|vtt` | Export the caption track as an SRT/WebVTT download, built from the same word timings the renderer burns in. |

### Search & analytics

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/search?q=&limit=` | One request, two result groups. Episodes match on title/description, clips on title/hook/summary/transcript text; always scoped to the caller's own rows. |
| `GET` | `/stats/overview` | Dashboard payload: episode/clip totals per status, source vs. output storage bytes, favourite count, average clip duration, top-5 clips by virality, recent jobs and the 7-day render count. |

### Public share links (no authentication)

Share tokens are 24 bytes of CSPRNG entropy (`secrets.token_urlsafe`). Every route fails with
`410 Gone` once a link is expired or revoked, and `404` when the underlying clip was deleted.
Responses never include the owner's identity or internal paths. All are rate limited.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/public/clips/{token}` | Public metadata: title, hook, summary, duration, score, keywords, thumbnail/download URLs, view count. Each call increments `view_count`. |
| `GET` | `/public/clips/{token}/download` | Stream the rendered MP4 (`409` if the clip has never been rendered). |
| `GET` | `/public/clips/{token}/thumbnail` | Stream the JPEG thumbnail for link previews. |

### Admin (superuser only)

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/admin/users?q=&skip=&limit=` | List accounts with episode/clip counters. |
| `PATCH` | `/admin/users/{user_id}` | Toggle `is_active` / `is_superuser` or edit `full_name`. Self-deactivation/self-demotion is rejected with `400`; changes invalidate the Redis user cache immediately. |

### Jobs

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/jobs` | Background jobs scoped to the caller's content. Filters: `episode_id`, `clip_id`, `job_type`, `status`, `limit`. |
| `GET` | `/jobs/{id}` | Job detail; refreshes state from the Celery result backend and upgrades `pending` → `running`/`success`/`failure` as the task settles. |
| `POST` | `/jobs/{id}/revoke?terminate=` | Ask Celery to cancel a pending or running job. |
| `GET` | `/jobs/worker/ping` | Round-trips a ping through the broker; returns online workers and the queue name. |

## Configuration matrix

All settings live in `core/config.py` and are read from `backend/.env`. Unknown keys are
ignored (`extra="ignore"`), and list fields accept comma-separated strings thanks to a
`mode="before"` validator combined with pydantic-settings' `NoDecode` annotation.

| Setting | Default | Purpose |
| --- | --- | --- |
| `PROJECT_NAME` | `Niche Podcast Clipper` | Shown in docs and the root banner. |
| `API_V1_PREFIX` | `/api/v1` | Prefix for all application routes. |
| `ENVIRONMENT` | `development` | `production`/`staging` enable hardening (below). |
| `DEBUG` | `false` | Enables Uvicorn auto-reload in `__main__`. |
| `ENABLE_DOCS` | `true` | Set `false` to hide `/docs`, `/redoc`, `/openapi.json`. |
| `FRONTEND_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | CORS allow-list **and** TrustedHost derivation. |
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/podcast_clipper` | SQLAlchemy DSN. |
| `AUTO_MIGRATE_DB` | `true` | Creates the schema on a fresh database and applies pending Alembic migrations automatically at startup. |
| `REDIS_URL` | `redis://localhost:6379/0` | Cache + rate-limit store. |
| `CELERY_BROKER_URL` | `redis://localhost:6379/1` | Task broker. |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6379/2` | Task result store. |
| `CACHE_TTL_SECONDS` | `3600` | Default cache entry lifetime. |
| `SECRET_KEY` | `change-me-...` | Access-token signing key. **Rejected at startup in production if left as a template value.** |
| `REFRESH_SECRET_KEY` | `change-me-...` | Refresh-token signing key (same production guard). |
| `ALGORITHM` | `HS256` | JWT algorithm. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | Access-token lifetime. |
| `REFRESH_TOKEN_EXPIRE_MINUTES` | `10080` (7 days) | Refresh-token lifetime. |
| `STORAGE_DIR` | `./media_storage` | Local media root (relative paths resolve against the backend package). |
| `MAX_UPLOAD_SIZE_MB` | `2048` | Upload cap enforced while streaming. |
| `ALLOWED_MEDIA_EXTENSIONS` | `.mp3,.mp4,.wav,.m4a,.mov,.mkv,.webm` | Upload allow-list (CSV in `.env`). |
| `ASR_MODEL_NAME` | `openai/whisper-base` | Speech-to-text checkpoint. |
| `HIGHLIGHT_MODEL_NAME` | `google/flan-t5-base` | Highlight scoring checkpoint. |
| `AI_DEVICE` | `cpu` | `cpu` or `cuda`/`cuda:N`; falls back to CPU when CUDA is unavailable. |
| `ASR_CHUNK_LENGTH_S` | `30` | Whisper chunk length. |
| `HF_HOME` | `./models` | Hugging Face cache directory. |
| `TRANSFORMERS_OFFLINE` | `false` | When true, models must already be cached (also skips startup warmup). |
| `OUTPUT_WIDTH` / `OUTPUT_HEIGHT` | `1080` / `1920` | Fallback output geometry for audio-only clips. |
| `OUTPUT_FPS` | `30` | Fallback output frame rate. |
| `RATE_LIMIT_REQUESTS` | `120` | Fixed-window request budget. |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Rate-limit window length. |
| `AUTH_RATE_LIMIT_REQUESTS` | `10` | Stricter per-IP budget for credential endpoints (register/token/login/refresh/logout). |
| `AUTH_RATE_LIMIT_WINDOW_SECONDS` | `60` | Window length for the strict auth limiter. |

## Security & middleware

**JWT.** Passwords are hashed with `passlib[bcrypt]`. Tokens are signed with `python-jose`.
`decode_token` checks the token *type* claim, so access and refresh tokens are not
interchangeable. `deps.get_current_user` distinguishes an expired token (`401`, "Access token
has expired") from an invalid one (`401`, "Could not validate credentials"), and returns `403`
for deactivated accounts.

**User cache.** `deps._load_user` reads through Redis (`user:{id}`, 300 s TTL) to avoid a
database round-trip on every authenticated request.

**Rate limiting.** `deps.rate_limit` implements a fixed-window counter in Redis keyed by client
IP, suffixed with a stable SHA-256 digest of the token when a Bearer header is present (so users
behind one NAT are not throttled collectively — and, unlike the previous per-process `hash()`,
the bucket is stable across restarts and workers). Exceeding the budget returns `429` with a
`Retry-After` header. It is applied as a router dependency on the expensive endpoints: upload,
transcribe, clip generation, render, batch render, public share routes and worker ping.
`deps.rate_limit_auth` applies a **much stricter per-IP budget** (default 10/minute) to every
credential endpoint so login attempts cannot be brute-forced at the general API pace. Both
limiters fail open when Redis is unavailable (the API stays usable; abuse protection pauses).

**Object ownership.** Every episode/clip route filters by `owner_id`. Job routes resolve
ownership through the job's clip **or** episode (a transcription job has no clip), so iterating
`/jobs/{id}` can no longer read another user's job — this closed an IDOR where episode-only
jobs skipped the check.

**Error masking.** The global `ValueError → 400` handler returns a generic `Invalid request`
body in production while the specific message stays in the logs; in development the message is
returned verbatim for fast debugging.

**Share links.** Tokens come from `secrets.token_urlsafe(24)` (≈190 bits of entropy), are unique
indexed, and resolve only while `revoked_at IS NULL` and `expires_at` (if set) is in the future.
Public responses expose no owner identity; downloads are rate limited and the MP4 must already
be rendered. Revocation (`DELETE /clips/{id}/shares/{share_id}`) takes effect immediately.

**Response hardening.** An HTTP middleware adds `X-Process-Time-Ms`, `X-App-Version`,
`X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer` to every response. In
production it additionally adds `X-Frame-Options: DENY`, a restrictive `Permissions-Policy`,
HSTS on HTTPS requests, `GZipMiddleware` (≥1 KB) and `TrustedHostMiddleware` derived from
`FRONTEND_ORIGINS`.

**Error contracts.** A `ValueError` handler converts uncaught validation errors into `400`
responses instead of `500`. Auth failures consistently raise the shared
`CREDENTIALS_EXCEPTION`, so media routes behave identically to JSON routes.

**Failure tolerance at boot.** Startup verifies PostgreSQL and Redis but never aborts:
`lifespan` logs actionable remediation ("password authentication failed", "Connection refused",
"Redis unavailable: caching and queuing are degraded"), so the API can still serve `/ping` and
`/health` while dependencies are down.

## Data model

```
User 1───┬──< Episode 1───┬── Transcript 1───< TranscriptSegment
         │                │        (unique per episode)
         │                └──< Clip ──┬──< RenderJob
         │                           └──< ClipShare  (tokenised public links)
         └───────────────────────────< Clip   (owner_id)
```

**`users`** — `email` (unique), `hashed_password`, `full_name`, `is_active`, `is_superuser`,
timestamps.

**`episodes`** — ownership (`owner_id`), display metadata (`title`, `description`,
`original_filename`, `stored_filename`), `media_path` (storage-relative), probed technical
metadata (`duration_seconds`, `width`, `height`, `fps`, `has_video_stream`, `media_type`,
`mime_type`, `file_size_bytes`), `status`, `error_message`.

**`transcripts`** — one per episode (unique `episode_id`), with `language`, `full_text`,
`model_name`, `duration_seconds`, `word_count` and ordered `TranscriptSegment` children
(`index`, `start_time`, `end_time`, `text`, `words`).

**`clips`** — the core editing entity:

- Boundaries: `start_time`, `end_time`, `duration_seconds`.
- Content: `title`, `hook`, `summary`, `transcript_text`, `words` (JSON word timings).
- Scores: `virality_score`, `hook_score`, `emotion_score`, `quotability_score`, `keywords`.
- Style: `caption_style`, `framing` (JSON, drive the OpenCV renderer).
- Render state: `status`, `progress`, `output_path`, `thumbnail_path`, `output_size_bytes`, `error_message`.
- Curation: `is_favorite` (star flag, toggled via `POST /clips/{id}/favorite`).

**`clip_shares`** — public share links: `clip_id`/`owner_id` (both FK CASCADE), `token`
(unique, 32-char urlsafe), `view_count`, `expires_at` (nullable), `revoked_at` (nullable),
`created_at`. Deleting a clip cascades to its links.

**`render_jobs`** — the Celery mirror: `job_type` (`transcription`, `highlight_extraction`,
`render`), `status` (`pending`, `running`, `success`, `failure`), `progress`,
`task_id`, `message`, `error_message`, `payload` (the render result dict), timestamps.

**Status enums** (`models/enums.py`): episodes `uploaded → queued → transcribing →
transcribed` (or `failed`); clips `draft → ready → rendering → rendered` (or `failed`).

## Caption styling & the render pipeline

### Presets

Five built-in themes ship in `services/caption_styles.py`, all exposed via
`GET /clips/presets`:

| Preset | Label | Character |
| --- | --- | --- |
| `tiktok` | TikTok Pop | Large centered bold text, pink highlight, uppercase. |
| `karaoke` | Karaoke Highlight | Bottom-anchored, lower-case, green word highlight, 4 words/line. |
| `minimal` | Minimal Clean | Small, bottom, translucent black background, no shadow. |
| `neon` | Neon Glow | Cyan text with magenta highlight, centered. |
| `bold_yellow` | Bold Yellow | Very large yellow uppercase text with a heavy outline. |

`resolve_preset()` falls back to the default (TikTok Pop) for unknown names.
`caption_style_to_dict()` / `framing_to_dict()` merge partial payloads onto the defaults, so
the API accepts sparse updates without losing fields.

### How a clip is rendered (`services/video/renderer.py`)

1. **Probe geometry.** Source width/height/FPS are preserved exactly — the output matches the
   source, with no crop, zoom, resize or aspect-ratio conversion. Odd dimensions are padded up
   by one pixel because H.264 requires even dimensions.
2. **Resolve a writer.** `_open_writer` tries `avc1`, then `H264`, then `mp4v`, suppressing
   OpenCV's C-level codec-probing noise by redirecting file descriptor 2.
3. **Build caption groups.** `_build_caption_groups` → `captions.words_from_payload` →
   `captions.group_caption_lines`. Captions are always repositioned to the bottom of the
   original frame.
4. **Frame loop.** Seeking to `start_time`, frames are read and captioned until the clip
   duration is reached. Caption drawing is skipped when the composited frame is unchanged,
   which keeps the reported `captions_burned` count meaningful.
5. **Restore audio.** The source segment's audio is extracted and muxed. If the video track was
   written as `mp4v`, `_reencode_to_h264` remuxes *and* attaches audio in a single pass.
6. **Verify & thumbnail.** `_verify_output` re-opens the file to prove it is decodable, then
   `_write_thumbnail` emits a JPEG.

Audio-only episodes take a different path: `render_audio_only` draws captions over a subtle
animated gradient so the clip still works on vertical feeds.

### Word timing resolution (important)

`captions.words_from_payload(words, clip_start, clip_end, fallback_text)` is the single source
of truth for caption text and timing:

- Persisted word timings are used when they still describe the current transcript.
- `fallback_text` is the clip's `transcript_text`. **When it disagrees with the stored word
  timings, the edited transcript wins** and timings are re-derived by distributing the new text
  evenly across the clip span.
- Comparison ignores case and punctuation (`_normalise_token`) and treats a word-count mismatch
  as stale (`_matches_fallback_text`).
- An empty transcript never wipes captions — the stored timings are kept.

This precedence is what keeps captions in sync after a transcript edit in the editor: the
render task passes the freshly persisted `clip.transcript_text`, so the burned captions are
rebuilt from the edited text. The editor's `caption-timeline` endpoint flows through the
identical code path, so what the preview draws is what the encoder produces.

> **Note.** Re-derived timings are evenly spaced rather than word-accurate, because the edited
> transcript carries no aligned word timestamps of its own. For word-perfect karaoke sync on
> edited clips, forced alignment against the audio would be the follow-up.

### Framing

`services/video/framing.py` provides `detect_focus_point` (face detection with a sensible
fallback), `FramingState` (smoothed focus tracking with clamping) and `compute_crop_box`.
`FramingOptions` exposes `mode` (`auto_face`, `center`, `manual`), `focus_x`/`focus_y`, `zoom`,
`smooth_tracking`, blur-background controls, `output_width`/`output_height`, `fps`,
`burn_captions` and `crf`. Note that the current renderer deliberately preserves native
geometry; these options are the supported configuration surface for the framing stage.

## Background processing (Celery)

**App configuration** (`core/celery_app.py`): JSON serialisation, UTC, `task_track_started`,
2-hour hard / 110-minute soft time limits, `worker_max_tasks_per_child=8` (keeps torch memory
bounded), `prefetch_multiplier=1`, 7-day result expiry, late acknowledgement with
`task_reject_on_worker_lost`, and a default queue named `clipper`.

**Tasks**

| Task | Purpose |
| --- | --- |
| `tasks.ai.transcribe_episode` | Runs Whisper locally, persists the transcript + segments + word timings, flips the episode status. |
| `tasks.ai.extract_highlights` | Builds candidate windows, scores them heuristically and with Flan-T5, then persists clips. |
| `tasks.ai.prewarm_models` | Beat task (every 6 hours) that pulls the models into memory ahead of demand. |
| `tasks.video.render_clip` | Burns captions and encodes the vertical clip (video or audio-only path). |

Both AI and rendering tasks write progress to the clip **and** the mirrored `RenderJob` row via
`_update_progress` / `_mark_job`, mark failures with a truncated error message, and retry once
(`max_retries=1`). Every task clears the relevant Redis cache prefixes on success so stale
payloads are never served. The **beat schedule** (`worker.py`) runs `prewarm-local-models`
every 6 hours.

## Local AI models

`services/ai/model_registry.py` holds lazy, thread-safe, process-wide singletons so the FastAPI
process and every worker share one copy of each model. `_resolve_device` maps the `AI_DEVICE`
string to the integer transformers expects and downgrades to CPU when CUDA is requested but
unavailable. Startup preloads models in a daemon thread (development only, and only when not
offline) so the API never blocks on a checkpoint download.

## Storage layout

```
media_storage/
├── episodes/<owner_id>/<episode_id>/<timestamp>_<uuid>.<ext>   source uploads
├── clips/<owner_id>/<clip_id>/                                 rendered MP4 + thumbnail
├── thumbnails/                                                 shared thumbnail space
├── audio/                                                      extracted/derived audio
└── tmp/                                                        scratch space
```

Paths are persisted **relative** to the storage root (`relative_media_path`) and resolved back
with `absolute_media_path`, which keeps the tree relocatable between machines and containers. A
relative `STORAGE_DIR` resolves against the backend package directory, not the process CWD.
Upload filenames are sanitised (directory components stripped, unsafe characters replaced) and
stored under a timestamped UUID name to prevent collisions and traversal.

## Database migrations

```bash
cd backend
alembic upgrade head                      # apply all migrations
alembic revision --autogenerate -m "msg"  # create a new migration
alembic downgrade -1                      # roll back one step
```

The migrations are cumulative:

| Revision | Purpose |
| --- | --- |
| `0001_initial_schema` | Users, episodes, transcripts (+ segments, candidates), clips, render jobs, indexes. |
| `0002_widen_media_size_columns` | `file_size_bytes` / `output_size_bytes` → `BIGINT` so the default 2048 MB upload limit cannot overflow a 32-bit column. |
| `0003_favorites_share_links` | Adds `clips.is_favorite` and the `clip_shares` table (token, view counter, expiry, revocation). |

## Testing & smoke checks

`backend/scripts/smoke_test.py` verifies the CPU-only helpers without needing models, a database
or a running server: candidate windowing, heuristics, highlight fallback, caption rendering,
framing math, storage path handling, **subtitle (SRT/VTT) generation** and **share-link
expiry/revocation gates**. Run it with:

```bash
cd backend && source venv/bin/activate
python scripts/smoke_test.py
```

Quick manual checks:

```bash
curl -s http://localhost:8000/ping
curl -s http://localhost:8000/api/v1/health        # now includes disk headroom
curl -s http://localhost:8000/api/v1/health/models
curl -s http://localhost:8000/api/v1/jobs/worker/ping
```

## New feature walkthrough (curl)

```bash
# 1. Star a clip and list only favourites
curl -X POST "http://localhost:8000/api/v1/clips/42/favorite" -H "Authorization: Bearer $TOKEN"
curl "http://localhost:8000/api/v1/clips?favorite=true" -H "Authorization: Bearer $TOKEN"

# 2. Export the library as CSV
curl -OJ "http://localhost:8000/api/v1/clips/export" -H "Authorization: Bearer $TOKEN"

# 3. Queue a batch render (duplicates collapse, impossible clips are reported)
curl -X POST "http://localhost:8000/api/v1/clips/render-batch" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"clip_ids": [42, 43, 44], "priority": 3}'

# 4. Download subtitles generated from the burned-caption word timings
curl -OJ "http://localhost:8000/api/v1/clips/42/captions-file?fmt=vtt" -H "Authorization: Bearer $TOKEN"

# 5. Create a public share link (7-day expiry) and use it without a token
SHARE_URL=$(curl -s -X POST "http://localhost:8000/api/v1/clips/42/shares" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"expires_in_days": 7}' | python -c "import sys,json;print(json.load(sys.stdin)['url'])")
curl -s "http://localhost:8000$SHARE_URL"                       # public metadata + view counter
curl -o clip.mp4 "http://localhost:8000$SHARE_URL/download"     # public download

# 6. Search the whole library in one call
curl "http://localhost:8000/api/v1/search?q=growth" -H "Authorization: Bearer $TOKEN"

# 7. Dashboard analytics
curl "http://localhost:8000/api/v1/stats/overview" -H "Authorization: Bearer $TOKEN"
```

> Share links can be revoked at any time with
> `DELETE /api/v1/clips/{id}/shares/{share_id}`; revoked or expired tokens answer `410 Gone`.
> The first superuser can be promoted manually:
> `sudo -u postgres psql -d podcast_clipper -c "UPDATE users SET is_superuser = true WHERE email = 'you@example.com';"`

## Deployment

`docker-compose.yml` provisions four services: `api` (Uvicorn, 2 workers, proxy headers),
`worker` (Celery, concurrency 4), `postgres:16-alpine` and `redis:7-alpine`, with named volumes
for `media_storage` and the HF `models` cache so checkpoints are not re-downloaded.

```bash
export POSTGRES_PASSWORD="<strong-password>"
docker compose up --build
```

Compose overrides `ENVIRONMENT=production`, `DEBUG=false`, `ENABLE_DOCS=false` for both the API
and the worker, **and pins the intra-network service hosts** — `DATABASE_URL` points at the
`postgres` service (using the required `POSTGRES_PASSWORD`), while `REDIS_URL`,
`CELERY_BROKER_URL` and `CELERY_RESULT_BACKEND` point at the `redis` service. That means a
developer `backend/.env` with `localhost` defaults can no longer silently break the containers.
The image runs as a non-root user (uid 10001) and ships a `HEALTHCHECK` against `/ping`. Before
exposing this externally you must:

- **Generate strong secrets** for `SECRET_KEY` and `REFRESH_SECRET_KEY` — the app refuses to
  start in production with template values, and this check now works even when the variables
  live only in the dotenv file (`openssl rand -hex 32` is the recommended generator).
- **Schema migrations** — `0003` adds `clips.is_favorite` and the `clip_shares` table, and
  `0002` widened the byte-size columns to `BIGINT`. The API applies pending migrations
  automatically at startup (`AUTO_MIGRATE_DB`, default `true`); set it to `false` and run
  `alembic upgrade head` manually if your deployment manages migrations externally.
- Point `DATABASE_URL` at the Compose Postgres instance (now done for you by the compose
  `environment` overrides described above).

---

# 🧱 Production hardening changelog

What was fixed or hardened in this release, beyond the feature work:

| Area | Change |
| --- | --- |
| **IDOR in `/jobs/{id}`** | Episode-only jobs (transcription / highlight extraction) previously skipped the ownership check, so any authenticated user could read another user's job — including its error message. The owner is now resolved through the clip *or* the episode and enforced for every job. |
| **Brute-force protection** | All credential endpoints (`/auth/register`, `/auth/token`, `/auth/login`, `/auth/refresh`, `/auth/logout`) now sit behind a strict per-IP fixed-window limiter (`AUTH_RATE_LIMIT_REQUESTS`, default 10/min) that is independent of the general API budget. |
| **Stable rate-limit buckets** | The per-token bucket key uses a SHA-256 digest instead of Python's salted `hash()`, whose per-process seed made the quota effectively random across workers and restarts. |
| **Error masking in production** | The global `ValueError → 400` handler no longer echoes raw exception text to clients in production; the detail is kept in the server log. |
| **DB pool resilience** | `pool_recycle=1800` joins `pool_pre_ping` so PostgreSQL restarts / idle firewalls cannot strand pooled connections. |
| **Compose service hosts** | `DATABASE_URL`, `REDIS_URL`, `CELERY_BROKER_URL` and `CELERY_RESULT_BACKEND` are overridden with container DNS names (and `POSTGRES_PASSWORD` interpolation) so a developer `.env` cannot point the containers at `localhost`. |
| **Repo hygiene** | Added `.gitignore`, `.dockerignore` and `backend/.env.example`. The Docker build context no longer ships model blobs, media files, `.env` secrets or virtualenvs; secrets cannot be committed. |
| **Health observability** | `/health` now reports the disk `total`/`free` bytes of the volume hosting `media_storage` — the first thing that fills up on a clipping server. |
| **Smoke coverage** | `scripts/smoke_test.py` gained subtitle-format checks (SRT separators, WebVTT header) and share-link gate checks (expiry/revocation), still with zero external dependencies. |

---

# 🎨 Frontend Documentation

## Stack & tooling

React 19 + Vite 8, `react-router-dom` v6, and axios. Styling is hand-written CSS (no framework)
split across `styles/base.css`, `styles/components.css`, `styles/pages.css`. Linting via ESLint
with the react-hooks and react-refresh plugins. Scripts: `npm run dev`, `npm run build`,
`npm run preview`, `npm run lint`.

**Dev proxy.** `vite.config.js` proxies `/api` and `/media` to `http://localhost:8000`, so the
SPA uses relative URLs and the browser never sees a CORS preflight in development.

## Directory layout

```
frontend/src/
├── main.jsx                    Root render: BrowserRouter → Toast → Auth → Clip providers
├── App.jsx                     Route table + SessionGate/ProtectedRoute/PublicOnlyRoute guards
├── index.css, App.css          Entry styles
├── styles/                     base.css, components.css, pages.css
├── api/
│   ├── client.js               axios instance, token storage, refresh interceptor, describeError
│   ├── auth.js                 register, login, token, me, updateProfile, logout
│   ├── episodes.js             list, get, upload (with progress), update, remove, transcribe, transcript, stats, mediaUrl
│   ├── clips.js                generate, list, get, update, remove, render, captionTimeline, preview, downloads, presets
│   └── jobs.js                 jobsApi (list/get/revoke/workerPing) + healthApi (status/models/worker)
├── context/
│   ├── AuthContext.jsx         JWT session state + login/register/logout/updateProfile
│   ├── ClipContext.jsx         Episodes/clips/presets state, upload+transcribe+generate+render flows, polling
│   └── ToastContext.jsx        Transient notifications
├── components/
│   ├── Layout.jsx              App shell: navigation, auth-aware header, toast host
│   ├── UploadPanel.jsx         Drag-and-drop upload with progress and auto-transcribe toggle
│   ├── EpisodeCard.jsx         Episode summary card with status and stats
│   ├── ClipCard.jsx            Clip preview card with score, status and actions
│   ├── TranscriptViewer.jsx    Timestamped transcript with word-level display
│   └── CaptionOverlay.jsx      Draft captions drawn on a canvas over the preview video
├── pages/
│   ├── HomePage.jsx            Marketing landing
│   ├── LoginPage.jsx           Sign-in form
│   ├── RegisterPage.jsx        Account creation form
│   ├── DashboardPage.jsx       Upload panel + episode grid
│   ├── EpisodeDetailPage.jsx   Transcript, clip generation, clip grid
│   ├── ClipDetailPage.jsx      Rendered clip playback, metadata, download
│   ├── ClipEditorPage.jsx      Caption/framing editor, source-media preview with draft captions, save + render
│   └── ProfilePage.jsx         Account settings
└── utils/format.js             Duration, timecode and byte formatting helpers
```

## Routing

| Path | Access | Page |
| --- | --- | --- |
| `/` | Public | Landing page. |
| `/login` | Public only (redirects authenticated users to `/dashboard`) | Sign in. |
| `/register` | Public only | Create account. |
| `/dashboard` | Protected | Upload + episode list. |
| `/episodes/:episodeId` | Protected | Transcript, highlight generation, clips. |
| `/clips/:clipId` | Protected | Rendered clip detail. |
| `/clips/:clipId/edit` | Protected | Caption / framing editor. |
| `/profile` | Protected | Profile settings. |
| `*` | — | Redirect to `/`. |

Three guard components in `App.jsx` implement this: `SessionGate` renders a "Checking session…"
placeholder while the stored token is validated; `ProtectedRoute` redirects unauthenticated
visitors to `/login`; `PublicOnlyRoute` bounces authenticated users away from the auth pages.

## API layer

`api/client.js` owns the transport concerns:

- **Base URL.** `VITE_API_BASE_URL` if set, otherwise the relative `/api/v1` (which the Vite
  proxy forwards). 120-second timeout to accommodate slow local inference.
- **Token storage.** `npc.access_token`, `npc.refresh_token` and `npc.user` in `localStorage`,
  managed through `getAccessToken`, `setTokens`, `clearTokens`, `getStoredUser` and `storeUser`.
- **Request interceptor.** Attaches `Authorization: Bearer <access_token>` when a token exists.
- **Response interceptor.** On `401` (excluding `/auth/*` endpoints) it performs a *single
  shared* refresh via `/auth/refresh` — concurrent failures await the same promise instead of
  stampeding the endpoint — then replays the original request once. If the refresh fails, it
  clears tokens and dispatches an `npc:signed-out` window event.
- **`describeError`.** Normalises axios failures into readable strings, including a friendly
  "Cannot reach the API. Is the FastAPI server running on port 8000?" for network errors.

Every module (auth, episodes, clips, jobs) is a thin, convention-typed wrapper over this
instance. Two patterns are worth noting:

- **Authenticated media URLs.** `<video>`/`<img>` tags cannot send headers, so `downloadUrl`,
  `thumbnailUrl` and `episodeMediaUrl` append `?token=<access_token>`.
- **Blob endpoints.** The preview frame and download endpoints are fetched with
  `responseType: 'blob'` and converted to `URL.createObjectURL` URLs, which the caller is
  responsible for revoking.

## State management

Three nested contexts (no Redux), composed in `main.jsx` as
`ToastProvider → AuthProvider → ClipProvider → App`.

**`AuthContext`** — holds `user`, `token` and `initialising`. On mount it validates a stored
token by calling `/auth/me`; on failure it clears the session. It listens for the
`npc:signed-out` event emitted by the axios interceptor, so a failed silent refresh also resets
React state. Exposes `isAuthenticated`, `login`, `register`, `signOut` and `updateProfile`.

**`ClipContext`** — the app's data hub. Owns `episodes`, `clips`, `presets`, `transcripts`,
`jobs`, `loading` and `uploadProgress`, and exposes the full workflow: `loadEpisodes`,
`loadClips`, `loadPresets`, `loadTranscript`, `uploadEpisode` (with progress), `deleteEpisode`,
`refreshEpisode`, `transcribeEpisode`, `generateClips`, `updateClip`, `deleteClip` and
`renderClip`. It also re-exports the stateless clip helpers (`previewFrame`, `downloadClip`,
`clipDownloadUrl`, `clipThumbnailUrl`, …) and the canonical `DEFAULT_CAPTION_STYLE` /
`DEFAULT_FRAMING` objects used to seed the editor.

**`renderClip` and polling.** Queueing a render starts a self-rescheduling `setTimeout` chain
(id tracked in a `pollTimers` ref, so it survives re-renders and is cleared on unmount) that
polls `/clips/{id}/status` every 1.8 s. On `success` it refetches the clip and shows a toast; on
`failure` it marks the clip failed and surfaces the error message; transient errors back off to
4 s.

## The clip editor

`ClipEditorPage` is the most involved screen and is built around a **draft** object seeded once
from the loaded clip (title, hook, summary, boundaries, transcript, caption style, framing).
Edits mutate the draft; nothing is persisted until save.

**Live preview.** The preview plays the episode's **source media** (`GET /episodes/{id}/media`)
as the moving picture and draws the draft captions over it on a `<canvas>` (`CaptionOverlay`).
A debounced effect keyed on the caption style, cut bounds and transcript fetches the whole
caption timeline once from `GET /clips/{id}/caption-timeline`; the overlay then redraws every
animation frame straight from `video.currentTime`, so captions change on the exact frame the word
does and playback issues no requests at all.

The moving picture is the *source*, not the rendered MP4, and that is what keeps a single caption
track on screen: the render has the previous captions burned into its pixels, so overlaying the
draft captions on it showed the old and new captions at once as soon as a preset changed the
styling. The source carries no captions. Playback is constrained to the clip's cut (start at the
in-point, loop at the out-point), and the clip-relative timeline is rebased onto the source clock
with the clip's in-point. Framing (zoom, blurred background fill, face tracking) is applied by the
encoder and is therefore approximated here rather than previewed exactly.

`GET /clips/{id}/preview` still renders a single composited JPEG frame through the real backend
OpenCV pipeline, for callers that want an exact still.

**Presets.** The preset gallery is loaded from `GET /clips/presets` (`loadPresets`), letting the
UI stay in sync with the backend theme definitions instead of duplicating them.

**Caption save & render workflow.**

1. The editor allows tweaking title, hook, summary, cut bounds, framing, caption styling, and
   editing the transcript text.
2. Clicking **Save changes** persists metadata, styling, and the edited transcript to `PATCH /clips/{id}`,
   then triggers `POST /clips/{id}/render` which queues a background render job.
3. The editor polls the render status until completed, updating the preview video once ready.
4. Because the persisting step writes the edited transcript and the renderer resolves captions from
   that same text, the burned captions always match the edited words.

**Rendered-clip invalidation.** `PATCH /clips/{id}` (used by the editor's save) discards the
previous render, so the UI correctly shows the clip as needing a re-render after any edit rather
than serving a stale MP4.

## Frontend build & deployment

```bash
cd frontend
npm install
npm run dev      # http://localhost:5173 with the API proxy
npm run build    # emits frontend/dist
npm run preview  # serve the production build locally
npm run lint
```

The production build is static, so serve `dist/` from any web server and set
`VITE_API_BASE_URL` to the absolute API origin at build time (the Vite dev proxy does not exist
in production, and the backend's CORS/TrustedHost configuration is derived from
`FRONTEND_ORIGINS` — keep the two in sync).

---

# 🧪 End-to-end walkthrough

```bash
# 1. Register and obtain a token
curl -s -X POST http://localhost:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"supersecret1","full_name":"You"}'

TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"supersecret1"}' | jq -r .access_token)

# 2. Upload an episode (queues transcription immediately)
curl -s -X POST http://localhost:8000/api/v1/episodes/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F file=@episode.mp3 -F title='Episode 1' -F auto_transcribe=true

# 3. Wait for the transcript, then fetch it
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/v1/episodes/1/transcript | jq '.segments | length'

# 4. Generate clips from the transcript
curl -s -X POST "http://localhost:8000/api/v1/clips/generate?episode_id=1" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"clip_count":5,"min_duration_seconds":15,"max_duration_seconds":60}'

# 5. Render a clip
curl -s -X POST http://localhost:8000/api/v1/clips/1/render \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"priority":5}'

# 6. Poll until the job succeeds, then download
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/clips/1/status | jq .status
curl -s -L -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/v1/clips/1/download -o clip.mp4
```

---

# 🩺 Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `SettingsError: error parsing value for field "FRONTEND_ORIGINS"` | Older `pydantic-settings` JSON-decoded the CSV list. Fixed in `core/config.py` via `NoDecode` + a CSV validator; run `pip install -r requirements.txt`. |
| `password authentication failed for user "postgres"` | Role password mismatch. Run `sudo -u postgres psql -f scripts/db_setup.sql`, or align `DATABASE_URL`. |
| `connection refused ... port 5432` | `sudo systemctl start postgresql`. |
| `Redis unavailable` at startup | `sudo systemctl start redis-server`. Caching and queues degrade but the API still boots. |
| Rendered MP4 plays in VLC but not the browser | The `mp4v` fallback was kept because the H.264 transcode did not produce an acceptable file; the worker log names the encoder and reason. Ensure `imageio-ffmpeg` is installed and check the `H.264 re-encode produced N audio streams` / `H.264 encoder ... unavailable` lines for the cause. |
| Upload *always* fails with 500 after registration | The `upload_episode` route was missing its `return` statement. Re-sync to `2b7f3d1` or apply the fix from `CHANGELOG`. |
| `ValueError: ... SECRET_KEY still contains a template value` | The application now refuses to start in production with the template JWT secrets (the previous check was silently bypassed when `ENVIRONMENT=production` came only from `.env`). Generate strong secrets: `openssl rand -hex 32` for both `SECRET_KEY` and `REFRESH_SECRET_KEY`. |
| Migration `0002` fails with `ERROR: type integer cannot be changed to bigint` | This occurs only on very old PostgreSQL versions (< 9.2). Run `ALTER TABLE episodes ALTER COLUMN file_size_bytes TYPE BIGINT; ALTER TABLE clips ALTER COLUMN output_size_bytes TYPE BIGINT;` manually. |
| Thumbnails show a broken image / 500 response | The `download_thumbnail` route previously fell through without returning a file. Update the backend (fixed in this revision). |
| Job stays `pending` forever | No worker is consuming the `clipper` queue. Start `celery -A worker.celery_app worker --loglevel=info` and confirm with `GET /api/v1/jobs/worker/ping`. |
| `Error: No such option '--autoreload'. Did you mean '--autoscale'?` | `--autoreload` is a Django-Celery feature, not a Celery 5.4 core worker option. Drop it (or use the `watchfiles` wrapper documented under *Celery worker*). |
| `429 Too many requests` | The fixed-window rate limit was hit. Wait `RATE_LIMIT_WINDOW_SECONDS` or raise `RATE_LIMIT_REQUESTS`. |
| Clips return `409` on generate | The episode has no transcript yet — run `/episodes/{id}/transcribe` first. |
| Preview image never changes | Confirm the API is reachable and that `burn_captions` is enabled in the framing options. |
| Downloaded clips lack audio | The source segment had no decodable audio track, or muxing was skipped. Check worker logs for `_extract_source_audio` / `_mux_audio` warnings. |

## 📄 License

Distributed under the **JUST License**. See `LICENSE` for details.
