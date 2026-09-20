# Deploying Open Notebook — Vercel (Frontend) + Long-Running Backend

This repository is a **full-stack monorepo**, not a single Next.js app:

| Component | Stack | Runs on Vercel? |
|---|---|---|
| Web UI (`frontend/`) | Next.js 16, React 19 | ✅ Yes — fully supported |
| REST API (`api/`, `open_notebook/`) | Python 3.12 FastAPI | ❌ No — needs long-running processes |
| Database | SurrealDB (WebSocket + GraphQL) | ❌ No — needs a reachable server |
| Background worker (`surreal-commands`) | Content ingestion, embeddings, podcast rendering (ffmpeg) | ❌ No — jobs run for minutes and need a persistent disk |

The frontend is Vercel-native. The recommended production topology is:

```
Browser
  │  https://your-app.vercel.app
  ▼
Vercel (Next.js frontend)                ── proxies /api/* via rewrites ──┐
                                                                          ▼
                                            FastAPI + worker (Railway / Fly.io / Render / VPS)
                                                                          │
                                                                          ▼
                                                     SurrealDB (Cloud, or same host)
```

The Vercel deployment proxies **all** `/api/*` traffic to the backend via Next.js rewrites
(`INTERNAL_API_URL`), so the browser only ever talks to your Vercel domain — **no CORS
configuration needed** and the backend can stay private.

---

## Architecture of this setup

Two env vars control how the frontend reaches the backend:

- **`INTERNAL_API_URL`** (server-side) — used by Next.js rewrites and the two SSE
  streaming route handlers (`/api/search/ask`, source chat messages). This is the only
  var **required** on Vercel. Point it at the backend's public HTTPS URL.
- **`API_URL`** (optional) — when set, the `/config` runtime endpoint tells the browser
  to call the backend **directly**, bypassing Vercel. Use this only if you hit Vercel
  proxy limits with large file uploads; the backend must then allow your Vercel origin
  in `CORS_ORIGINS`.

Runtime behavior added for Vercel (in `frontend/src/app/config/route.ts`): when `VERCEL=1`
(set automatically by Vercel) and no `API_URL` is configured, the endpoint returns an
empty API URL so the app uses relative paths and everything flows through the rewrites
proxy — zero-config, as long as `INTERNAL_API_URL` is set.

`frontend/next.config.ts` also skips `output: "standalone"` when building on Vercel
(Vercel uses its own output format; standalone remains the default everywhere else, so
the Docker build is unaffected).

---

## Step 1 — Deploy the backend (Railway is the easiest)

The existing multi-stage `Dockerfile` works on any Docker host (Railway, Fly.io, Render,
Easypanel, a VPS). Pick **one** of these two variants:

### Variant A: App + SurrealDB in one container (simplest)

Build the `single` target, which bundles SurrealDB via supervisord:

- **Railway**: New Project → Deploy from GitHub repo → Settings → Build →
  Custom build command: `docker build --target single -t app .`
  (or set `RAILWAY_DOCKERFILE_PATH` and use a small Dockerfile override:
  `FROM lfnovo/open_notebook:latest-single`).
- Attach a **volume** mounted at `/app/data` (uploads, podcasts, checkpoints live here).
- Set environment variables:

  ```env
  SURREAL_URL=ws://localhost:8000/rpc
  SURREAL_USER=<strong-user>
  SURREAL_PASSWORD=<strong-password>
  SURREAL_NAMESPACE=open_notebook
  SURREAL_DATABASE=open_notebook
  OPEN_NOTEBOOK_ENCRYPTION_KEY=<random-32-char-string>
  OPEN_NOTEBOOK_PASSWORD=<login-password-for-the-UI>   # strongly recommended for a public URL
  CORS_ORIGINS=https://your-app.vercel.app             # only needed if you use API_URL direct mode
  ```

- Expose the service on the default HTTP port (80 → container port `8502`).

### Variant B: External SurrealDB (cleaner for scale)

- Create a SurrealDB instance: [SurrealDB Cloud](https://surrealdb.com/cloud) (free tier,
  gives you a `wss://...` endpoint) or a second Railway service from the
  `surrealdb/surrealdb:v2` image with `SURREAL_EXPERIMENTAL_GRAPHQL=true`.
- Deploy this repo with the default Dockerfile target (`runtime`), setting:

  ```env
  SURREAL_URL=wss://<your-instance>.surreal.cloud/rpc   # or ws://surrealdb:8000/rpc in-container
  SURREAL_USER=...
  SURREAL_PASSWORD=...
  SURREAL_NAMESPACE=open_notebook
  SURREAL_DATABASE=open_notebook
  OPEN_NOTEBOOK_ENCRYPTION_KEY=...
  OPEN_NOTEBOOK_PASSWORD=...
  ```

- Still attach a volume at `/app/data` — the podcast pipeline and uploads need a real
  filesystem. **This is the main reason the backend cannot be a Vercel function.**

Verify the backend: `curl https://your-backend.up.railway.app/api/config` should return
JSON with `"version"`.

## Step 2 — Deploy the frontend on Vercel

1. Push this repository (or your fork with the `vercel-deploy` branch merged) to GitHub.
2. In Vercel: **Add New → Project → Import** your repo.
3. Set **Root Directory** to `frontend` (Vercel auto-detects Next.js).
4. Add the environment variable:

   | Name | Value | Notes |
   |---|---|---|
   | `INTERNAL_API_URL` | `https://your-backend.up.railway.app` | **Required.** Public URL of the backend, no trailing slash |
   | `API_URL` | *(leave empty)* | Only set for direct browser→backend mode (needs `CORS_ORIGINS` on the backend) |

5. Deploy. `frontend/vercel.json` already sets:
   - `framework: nextjs`
   - `maxDuration: 300` for the API route handlers (SSE chat/ask streaming) — Hobby plan
     clamps this to its own max; raise your plan for longer streams.
   - 1024 MB memory for those functions (SSE buffering headroom).

## Step 3 — Configure AI models in the UI

Open `https://your-app.vercel.app`, log in, then go to **Manage → Models** and add
credentials + models (see the AI APIs section below). Credentials are stored encrypted
in SurrealDB (that's what `OPEN_NOTEBOOK_ENCRYPTION_KEY` protects — back it up, it is
not recoverable).

---

## Limitations & gotchas on this topology

- **File uploads through the Vercel proxy**: Vercel's edge proxy has request body
  limits well below the backend's 100 MB setting. If large PDF/audio uploads fail,
  either set `API_URL` to the backend URL (direct browser→backend upload) or keep heavy
  media ingestion on a self-hosted instance.
- **Podcast generation latency**: episodes render in a background worker on the backend
  host and can take minutes; the UI polls job status, so this works fine — just don't
  scale the backend to zero.
- **Long-running SSE streams**: chat/ask streams pass through a Vercel function with
  `maxDuration: 300`; on Hobby the effective cap is lower (see your plan limits).
- **Costs**: Vercel Hobby (free) covers the frontend for personal use; Railway's trial +
  ~$5/mo covers a small backend; SurrealDB Cloud free tier or +$5/mo for the DB.

---

## Local development with this setup

```bash
# Terminal 1 — backend (needs a reachable SurrealDB, e.g. docker compose up surrealdb)
uv sync && uv run uvicorn api.main:app --port 5055
# + run the worker: uv run python -m surreal_commands.run_worker --only-commands open_notebook

# Terminal 2 — frontend
cd frontend && npm ci && npm run dev   # http://localhost:3000
```

Set `INTERNAL_API_URL` only if your backend is not on `localhost:5055`.
