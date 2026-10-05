<div align="center">

# Nehchat Agent · Slot Edition

**10 Independent Save Slots · Single / Dual-Model Role-Play · SSE Streaming · Self-Managed OpenAI-Compatible Model Catalog**

[简体中文](./README.md) · [English](./README.en.md)

![Python](https://img.shields.io/badge/Python-3.14+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-latest-009688?logo=fastapi&logoColor=white)
![MySQL](https://img.shields.io/badge/MySQL-8-4479A1?logo=mysql&logoColor=white)
![Vite](https://img.shields.io/badge/Vite-6-646CFF?logo=vite&logoColor=white)
![Streaming](https://img.shields.io/badge/Streaming-SSE-f97316)

</div>

---

A ready-to-run AI chat application: add any OpenAI-compatible provider and models yourself in "Model Settings", and manage multiple independent conversations across 10 save slots. Supports single-model chat and dual-model role-play (the two models reply in turn and relay to each other), with full SSE streaming, MySQL persistence, multi-user accounts with per-user data isolation, theme switching, custom background images, Markdown rendering, and complete backup/restore.

Frontend: Vite + vanilla JS. Backend: FastAPI + MySQL. One-click scripts to start; built-in account login with per-user data isolation — open registration via an invite code, plus an optional Basic Auth layer for public tunnels.

## Table of Contents

- [Features](#-features)
- [Architecture](#️-architecture)
- [Quick Start](#-quick-start)
- [Configuration](#️-configuration)
- [Providers & Models](#️-providers--models)
- [Dual-Model Role-Play](#-dual-model-role-play)
- [Themes & Backgrounds](#-themes--backgrounds)
- [Project Structure](#-project-structure)
- [API Reference](#-api-reference)
- [SSE Event Stream](#-sse-event-stream)
- [Development](#️-development)
- [FAQ](#-faq)
- [Security & License](#-security--license)

## ✨ Features

### 👥 Multi-User & Accounts

- **First-run setup**: create the admin account on first visit (localhost only); existing data is claimed by that account automatically
- **Invite-code registration**: set `INVITE_CODE` in `.env` to open registration; when unset, only existing accounts can sign in
- **Per-user isolation**: slots, messages, providers, and the model catalog are isolated per user
- **Secure storage**: scrypt-salted password hashes; HttpOnly cookie sessions (30 days) with logout
- **Optional outer gate**: add `WEB_USER` / `WEB_PASSWORD` for an extra HTTP Basic Auth layer on public tunnels

### 🗂️ Save Slots

- **10 independent slots**: each slot owns its own `model / system_prompt / params / dual_config` — run multiple conversations in parallel without interference
- **6-step creation wizard**: mode → model → params → prompt → model 2 → model 2 params; creation is blocked while the catalog is empty
- **Hot model swap**: `PATCH /api/slots/{i}/config` atomically updates model/prompt/params without touching history; both sides of a dual slot are fully independent
- **Auto title**: generated from the user's first message after the first turn; rename in place from the sidebar

### 💬 Chat

- **SSE streaming**: `POST /api/chat` and `POST /api/slots/{i}/chat/continue` render tokens in real time, with cancel (`■`) and rollback-on-interrupt
- **Continue (⏩)**: single mode appends to the last assistant reply; dual mode acts as an empty user turn — both roles reply once more
- **Fine-grained messages**: edit (auto-resend after save), delete precisely by ID, regenerate (`↻`, removes that round and resends), clear chat
- **Context window**: the most recent 100 messages are sent to the model each turn
- **Markdown rendering**: `marked` + `highlight.js` syntax highlighting, one-click code copy

### 🏷️ Model Catalog

- **Self-managed providers**: add any OpenAI-compatible endpoint in "Model Settings" (base URL, key or env var, model-id); slots only pick from the catalog
- **Connectivity test**: send a `hello` probe per saved model and get latency plus a reply preview
- **Key management**: keys live on the provider — stored directly or read from an environment variable; `GET /api/env-check` reports key readiness
- **Reference protection**: providers/models referenced by slots cannot be deleted; create matching models before importing a backup

### 🎨 Personalization

- **5 color themes**: Cosmic (default) / Emerald / Sunset / Sakura / Ocean — one-click switching
- **Light & dark mode**: segmented toggle, follows system preference on first visit, persisted in localStorage
- **Custom background**: upload / select / delete the current account's background with opacity and blur controls (≤10MB, jpg/png/webp/gif)

### 🔒 Reliability

- **Concurrency guard**: in-process `asyncio.Lock` + MySQL `GET_LOCK(ai_chat_slot_*)`; conflicts return a friendly `slot_busy` message
- **Smart retry**: no retry on 4xx; exponential backoff on 5xx / timeout / network errors; no retry once the stream has started (prevents duplicated content)
- **Failure rollback**: persisted messages are rolled back automatically on stream interruption (`delete_messages_from`); if model 2 fails, model 1's completed reply is kept
- **Full backup**: export a fully restorable JSON backup (version 2) or Markdown export; import into any empty slot

## 🏗️ Architecture

```
┌────────────────── Browser (static frontend built by Vite) ──────────────────┐
│  Slot grid ⇄ Chat view · 6-step wizard · Model settings · Theme/background  │
│  api.js: fetch + exponential backoff     chat.js: SSE parsing & dispatch    │
└────────────────────────────────────┬────────────────────────────────────────┘
                                     │ HTTP / SSE  (127.0.0.1:8000)
┌────────────────────────────────────▼────────────────────────────────────────┐
│ FastAPI (main.py lifespan initializes global singletons)                    │
│   routes/  slots · chat · models · catalog · backgrounds                    │
│   helpers/ resolve_slot · get_runtime (model key → runtime config) · error  │
│   clients/ AIClient — AsyncOpenAI streaming + retries                       │
│   state/   AIClient / SlotManager singletons                                │
│   session_manager/ SlotManager — tx template · pool · GET_LOCK              │
└────────────────────────────────────┬────────────────────────────────────────┘
                                     │ pymysql + DBUtils pool
                              ┌──────▼──────┐
                              │   MySQL 8   │  slots · messages
                              │ (auto-setup)│  providers · catalog_models
                              └─────────────┘
```

## 🚀 Quick Start

### Requirements

| Dependency | Version | Purpose |
|---|---|---|
| Python | 3.14+ | Backend runtime |
| MySQL | 8.x | Data storage (service must be running) |
| Node.js | 18+ | Only for frontend build / development |

### Steps (Windows)

1. **Configure the database**: create a `.env` file in the project root (see [Configuration](#️-configuration)); `MYSQL_PASSWORD` is required
2. **First run / after dependency changes**: double-click `重置启动.bat` (Reset & Start) — installs backend deps, builds the frontend, starts the server with `--reload`, and opens your browser
3. **Daily start**: double-click `快速启动.bat` (Quick Start) — checks port 8000, starts the backend, and opens your browser automatically
4. **First visit**: the page asks you to create the admin account (localhost only); existing data is claimed automatically. To invite friends, set `INVITE_CODE` in `.env`

> The `ai_chat` database and all tables (`slots` / `messages` / `providers` / `catalog_models`) are **created automatically** on first startup, including column-level migrations for older schemas — no manual SQL needed.

### Manual Start

```bash
cd backend
python -m uvicorn main:app --host 127.0.0.1 --port 8000
# Visit http://localhost:8000
```

### Public Access (optional)

Double-click `公网隧道.bat` (Public Tunnel): it starts the backend and creates a Cloudflare quick tunnel (`cloudflared`), printing a public `https://*.trycloudflare.com` URL. The service still runs on your machine; closing the window tears the tunnel down. All data requires account login; for an extra gate, set `WEB_USER` / `WEB_PASSWORD` in `.env` to add HTTP Basic Auth.

## ⚙️ Configuration

Set via the root `.env` file or system environment variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `MYSQL_HOST` | No | `localhost` | MySQL host |
| `MYSQL_PORT` | No | `3306` | MySQL port |
| `MYSQL_USER` | No | `root` | MySQL user |
| `MYSQL_PASSWORD` | **Yes** | — | Startup fails when unset |
| `MYSQL_DATABASE` | No | `ai_chat` | Database name (auto-created) |
| `ALLOWED_ORIGINS` | No | `http://localhost:5173,...` | CORS allowlist, comma-separated |
| `INVITE_CODE` | No | — | Registration invite code; when unset only existing accounts can sign in |
| `WEB_USER` | No | — | Optional Basic Auth outer-gate username (stacked with account login) |
| `WEB_PASSWORD` | No | — | Optional Basic Auth outer-gate password |
| `UVICORN_HOST` | No | `127.0.0.1` | Listen address (when using `python main.py`) |
| `UVICORN_RELOAD` | No | `0` | Hot-reload toggle (when using `python main.py`) |

> Provider key env vars: after checking "Read from environment variable" for a provider in Model Settings, set a variable with the name you specified (e.g. `MYPROVIDER_API_KEY=sk-xxxx`); it takes effect after the next process start.

**Generation params**: slots can preset `temperature / top_p / presence_penalty / frequency_penalty / num_predict`, etc. Only OpenAI-standard params are actually sent (`num_predict` maps to `max_tokens`, capped at 8192). The rest (`min_p / top_k / repeat_penalty / num_ctx`) are stored for reference only and never sent to incompatible endpoints.

## 🏷️ Providers & Models

No providers are built in. Open "Model Settings" in the top-right corner and add your own:

1. **Provider ID**: lowercase letters/digits/hyphens/underscores; immutable after creation
2. **Display name + base URL**: any OpenAI-compatible endpoint, e.g. `https://api.example.com/v1`, or local Ollama `http://127.0.0.1:11434/v1`
3. **API key**: may be empty (for local no-auth services), or check "Read from environment variable" and enter the variable name
4. **Add models**: `model-id` (the real name sent to the API) + display name; add as many as you like
5. **Test**: click "Test" next to a saved model to send `hello` and verify connectivity

Slots reference models by key `{provider_slug}:{model_id}`; at request time `get_runtime` resolves it to the actual `base_url + model_id + api_key`.

## 🎭 Dual-Model Role-Play

A dual slot is driven by `dual_config`; each model has its own model, prompt, params, and role name:

| Setting | Values | Behavior |
|---|---|---|
| `response_mode` | `both` / `model1` / `model2` | Both reply / only model 1 / only model 2 — switchable from the sidebar |
| `first_model` | `model1` / `model2` | Who replies first in each turn |
| `pass_mode` | `user` / `assistant` | How model 1's reply is passed to model 2 |

**Relay mechanism** (how model 2 sees model 1's reply):

- `pass_mode: user` (default): model 1's reply is merged into the user message as `[role name's reply]`, avoiding two consecutive user messages
- `pass_mode: assistant`: model 1's reply is passed as a separate `assistant` message (`role name: content`)

**Event order**: `model_start(role) → chunk* → model_done(role, message_id) → … → done(user_message_id, message_ids)`; the frontend renders bubbles with role badges (🎭 / 🌟) accordingly.

## 🎨 Themes & Backgrounds

- The theme button in the top-right corner (on both the grid and chat views) opens the theme panel: a light/dark segmented toggle on top and 5 color themes below
- The "Custom Background" section at the bottom of the panel: upload an image (≤10MB) or pick an existing one, and adjust opacity (0–100%) and blur (0–20px) in real time
- Uploads live in `backend/backgrounds/<user_id>/` and are served from `/backgrounds/{name}` after login; you can also drop shared, read-only images into `backend/backgrounds/`
- All preferences (theme / mode / background) live in browser localStorage; an inline script applies them before first paint to avoid flashing

## 📂 Project Structure

```
AI_project/
├─ backend/
│  ├─ main.py             # FastAPI entry: lifespan init, router registration, static hosting
│  ├─ config.py           # MySQL / slots / context window / backgrounds / default params
│  ├─ clients.py          # AIClient: streaming calls, hello test, retries & error mapping
│  ├─ auth.py             # Optional Basic Auth middleware + session dependency
│  ├─ accounts.py         # Accounts & sessions: scrypt hashing, login, legacy data claim
│  ├─ helpers.py          # error / resolve_slot / get_runtime / secret resolution
│  ├─ models.py           # Pydantic request/response models
│  ├─ session_manager.py  # SlotManager: DB bootstrap, CRUD, catalog, locks & transactions
│  ├─ state.py            # Global singletons
│  ├─ routes/
│  │  ├─ auth.py          # Setup / invite registration / login / logout / me
│  │  ├─ slots.py         # Slot CRUD, message ops, config update, backup import/export
│  │  ├─ chat.py          # SSE streams for /api/chat and continue (single/dual)
│  │  ├─ models.py        # Model list / env-check / default params
│  │  ├─ catalog.py       # Provider & catalog model CRUD, hello test
│  │  └─ backgrounds.py   # Background upload / list / delete
│  └─ backgrounds/        # Custom background directory (created at runtime)
├─ frontend/
│  ├─ index.html          # Grid view + chat view + wizard + modals
│  ├─ js/
│  │  ├─ main.js          # Entry: event binding & module wiring
│  │  ├─ api.js           # apiFetch: exponential backoff (no retry on 4xx)
│  │  ├─ state.js         # Global state
│  │  ├─ chat.js          # SSE parsing, send/continue/cancel/regenerate/edit
│  │  ├─ auth.js          # Login / register / first-run setup view & 401 handling
│  │  ├─ catalog.js       # Model settings modal (provider/model CRUD & test)
│  │  ├─ modals.js        # 6-step creation wizard (reused for editing)
│  │  ├─ theme.js         # Themes & light/dark mode
│  │  ├─ background.js    # Custom background management
│  │  └─ ui / markdown / confirm / toast / utils
│  ├─ css/                # variables / themes / base / chat / modals / ...
│  └─ vite.config.js      # dev 5173 proxies /api and /backgrounds → 8000
├─ 快速启动.bat            # Quick Start: port check + start server + open browser
├─ 重置启动.bat            # Reset & Start: install deps + build frontend + --reload
├─ 公网隧道.bat            # Public tunnel: cloudflared + backend + account login
└─ .env                   # Local environment variables (gitignored)
```

## 🔌 API Reference

Errors are uniform `{code, message, detail}`; the frontend maps them to localized toasts / inline cards. Except for `/api/auth/*`, every `/api` endpoint requires a session cookie.

### Accounts

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/auth/status` | Initialized / registration-enabled flags (no login needed) |
| `POST` | `/api/auth/setup` | First-run admin creation (localhost only, claims legacy data) |
| `POST` | `/api/auth/register` | Invite-code registration |
| `POST` | `/api/auth/login` | Login (sets session cookie) |
| `POST` | `/api/auth/logout` | Log out current session |
| `GET` | `/api/auth/me` | Current user |

### Slots & Messages

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/slots` | Overview of all 10 slots (`message_count` / `dual_enabled`, etc.) |
| `POST` | `/api/slots/{i}` | Create a slot (`CreateSlotRequest`, incl. dual branch) |
| `DELETE` | `/api/slots/{i}` | Delete a slot (messages cascade) |
| `GET` | `/api/slots/{i}/chat` | Slot detail + `history[{id, role, content, source}]` |
| `POST` | `/api/slots/{i}/chat/clear` | Clear the conversation |
| `DELETE` | `/api/slots/{i}/chat/messages` | Delete messages (prefers `from_id`, falls back to `from_index/to_index`) |
| `PATCH` | `/api/slots/{i}/chat/messages` | Edit a message (`message_id` preferred) |
| `PATCH` | `/api/slots/{i}/title` | Rename the slot |
| `PATCH` | `/api/slots/{i}/config` | **Hot model swap** (atomic update; dual sides independent) |
| `PATCH` | `/api/slots/{i}/dual-toggle` | Switch `response_mode / first_model` |
| `PATCH` | `/api/slots/{i}/api-key` | Deprecated (410 — keys are managed per provider now) |

### Chat Streams

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/chat` | Streaming chat SSE (single/dual routing, auto rollback on failure) |
| `POST` | `/api/slots/{i}/chat/continue` | Continue reply (single: merged continuation; dual: both reply a round) |
| `POST` | `/api/slots/{i}/chat/cancel` | Cancel current streaming reply and release lock |

### Model Catalog

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/models` | All models in the catalog (with provider info) |
| `GET` | `/api/env-check` | Whether each provider's key resolves at runtime |
| `GET` | `/api/default-params` | Default generation params |
| `GET` / `POST` | `/api/providers` | List providers (keys masked) / create |
| `PATCH` / `DELETE` | `/api/providers/{id}` | Update (slug immutable) / delete (when unreferenced) |
| `POST` | `/api/providers/{id}/models` | Add a model |
| `PATCH` / `DELETE` | `/api/providers/{id}/models/{mid}` | Update / delete a model |
| `POST` | `/api/providers/{id}/models/{mid}/test` | Non-streaming `hello` connectivity test |

### Backup & Backgrounds

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/slots/{i}/chat/export` | Export conversation as Markdown data |
| `GET` | `/api/slots/{i}/backup` | Export a full JSON backup (version 2) |
| `POST` | `/api/slots/{i}/backup` | Import a backup into an empty slot |
| `GET` / `POST` | `/api/backgrounds` | List / upload backgrounds (≤10MB) |
| `DELETE` | `/api/backgrounds/{name}` | Delete a background image |

## 📡 SSE Event Stream

`Content-Type: text/event-stream`, each frame is `data: {json}`:

| Event | Payload | Description |
|---|---|---|
| `model_start` | `role, name, icon` | A model starts generating (fired per model in dual mode) |
| `chunk` | `content, role?` | Incremental text fragment |
| `model_done` | `role, name, icon, message_id` | One model finished and persisted |
| `continue_start` | `message_id` | Single-model continuation begins (appended to that message) |
| `done` | `user_message_id, message_ids / assistant_message_id` | Whole turn completed |
| `error` | `code, content, key_target?` | Failure (`slot_busy` / `auth_failed` / `rate_limited` / `stream_interrupted` etc.) with the rollback anchor ID |

## 🛠️ Development

```bash
# Frontend development (dev server proxies /api and /backgrounds → 8000)
cd frontend
npm install
npm run dev          # http://localhost:5173

# Backend development
cd backend
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload

# Production build (backend serves frontend/dist first when present)
cd frontend
npm run build
```

Schema changes are migrated automatically by column detection in `_init_tables` (e.g. `slots.params`, `messages.source`, `slots.model VARCHAR(192)`); just restart after upgrading the code.

## ❓ FAQ

- **MySQL connection failure on startup**: make sure `MYSQL_PASSWORD` is set, the MySQL service is running, and host/port are correct; the server auto-creates the database but the user needs CREATE privileges
- **No models to pick when creating a slot**: add a provider and models in "Model Settings" first
- **Local Ollama**: base URL `http://127.0.0.1:11434/v1`, key can be empty; the service must be running and the model pulled
- **401 / auth failure in chat**: check the provider's key or env var in Model Settings; the per-model "Test" button helps debug quickly
- **Stream interrupted**: no retry after the stream starts (to avoid duplication); you'll see "The reply was interrupted mid-generation, please retry" — click `⏩ Continue` or resend
- **"Slot is busy"**: another request is using the same slot; wait for the previous turn to finish — the dual lock guarantees no interleaved writes
- **Port 8000 in use**: Quick Start detects the occupation and simply opens the existing service instead of starting a duplicate

## 🔒 Security & License

- The project has **built-in account login and per-user data isolation**; the server listens on `127.0.0.1` only. When exposing it publicly, consider adding `WEB_USER` / `WEB_PASSWORD` for an extra Basic Auth gate
- Passwords are stored as scrypt-salted hashes; API keys are stored in plain text in MySQL (or injected via environment variables); secure your machine accordingly
- No open-source license declared; for local personal use only

---

<div align="center">

> See also: [Backend runtime notes (Chinese)](./backend/README.md)

</div>
