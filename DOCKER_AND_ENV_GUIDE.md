# 🐳 Docker & Environment Setup Guide

This guide explains how to run the Outreach Pipeline locally using Docker, how environment files work, how local Docker interacts with Supabase, and how to collaborate safely.

---

## ❓ Frequently Asked Questions

### Does local Docker connect to the same Supabase database?
**YES.**

When you run the app inside Docker locally, the container makes outgoing HTTPS requests to the `SUPABASE_URL` specified in your `.env` file (`https://yflxaxidedjpancbffln.supabase.co`).
- Any lead uploaded, status changed, or template created locally is **immediately saved in the shared Supabase database**.
- Both your local Docker container (`http://localhost:8000`) and the Railway production server (`https://outreach-pipeline-production.up.railway.app`) read and write to the **exact same live database**.

### ⚠️ Critical Safety Setting: Avoid Duplicate Email Dispatching
Because both your local Docker container and Railway connect to the same Supabase database, **do not leave the background sending worker enabled locally**. If a campaign becomes due, both your computer and Railway might attempt to dispatch the same email.

In your **local** `.env` file, always set:
```env
# Disable local email dispatching (Railway handles sending in production)
SCHEDULER_ENABLED=false

# Optional safety net: prevent real emails from ever leaving your machine
DRY_RUN=true
```

---

## 🚀 Quickstart: Running with Docker

### Prerequisites
1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) (macOS / Windows / Linux).
2. Ensure Docker Desktop is running.

### 1. Clone the Repository
```bash
git clone https://github.com/priyanshu73/outreach-pipeline.git
cd outreach-pipeline
```

### 2. Configure Your `.env` File
Create your local `.env` by copying `.env.example`:
```bash
cp .env.example .env
```
Open `.env` and paste the shared project keys (see the [Environment Variables Breakdown](#-environment-variables-breakdown) below).

### 3. Start the Docker Container
```bash
docker compose up --build
```

### 4. Open in Your Browser
- Visit: **[http://localhost:8000](http://localhost:8000)**
- Log in using your `ADMIN_EMAIL` and `ADMIN_PASSWORD` from `.env`.

### 🔄 Live Hot-Reloading
The `docker-compose.yml` mounts the `./app` directory into the container. Any code changes you make to Python files or HTML templates will **automatically hot-reload** in your browser without restarting Docker.

### 🛑 Stopping the Container
Press `Ctrl + C` in your terminal, or run:
```bash
docker compose down
```

---

## 🔑 Environment Variables Breakdown

Your `.env` file controls all integrations and safety flags:

### 1. Database Configuration
```env
# "supabase" connects to the shared cloud Postgres instance.
# "sqlite" can be used for zero-network offline sandbox testing.
DATABASE_BACKEND=supabase
SUPABASE_URL=https://yflxaxidedjpancbffln.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-supabase-service-role-key
```

### 2. Admin Authentication & Session Encryption
```env
ADMIN_EMAIL=contractorops.ai@gmail.com
ADMIN_PASSWORD=your-secure-admin-password
SESSION_SECRET=long-random-string-for-cookie-signing
ENCRYPTION_KEY=fernet-key-used-to-encrypt-gmail-app-passwords-in-db
```

### 3. Safety & Schedulers
```env
# In local development: set SCHEDULER_ENABLED=false so your laptop doesn't send emails.
# On Railway production: set SCHEDULER_ENABLED=true so the server sends due emails.
SCHEDULER_ENABLED=false
DRY_RUN=false
```

### 4. AI & Scraping
```env
GEMINI_API_KEY=your-gemini-api-key
GEMINI_MODEL=gemini-flash-latest
MAPBOX_ACCESS_TOKEN=pk.your-mapbox-token
SERP_PROVIDER=serper
SERP_API_KEY=your-serper-api-key
```

### 5. Email Providers
```env
# "gmail" uses connected Gmail accounts; "pingram" uses the multi-rep Pingram API
EMAIL_PROVIDER=gmail
PINGRAM_API_KEY=pingram_sk_your_key
PINGRAM_API_URL=https://api.pingram.io/email
PINGRAM_NOTIFICATION_TYPE=outreach_campaign
FROM_DOMAIN=contractorops.ai
REPLY_TO=hello@contractorops.ai
```

---

## 👥 Multi-Developer Team Workflow

### Working with Git & Railway

```
                   ┌───────────────────────────────┐
                   │  Shared Supabase Database     │
                   │  (All leads, templates, etc.) │
                   └──────────────▲────────────────┘
                                  │
                 ┌────────────────┴────────────────┐
                 │                                 │
                 ▼ (Reads/Writes)                  ▼ (Reads/Writes)
      ┌──────────────────────┐          ┌──────────────────────┐
      │  Developer A (Local) │          │  Developer B (Local) │
      │  Docker on port 8000 │          │  Docker on port 8000 │
      │  SCHEDULER=false     │          │  SCHEDULER=false     │
      └──────────┬───────────┘          └──────────┬───────────┘
                 │                                 │
                 │   git push / pull               │
                 ▼                                 ▼
      ┌────────────────────────────────────────────────────────┐
      │     Private GitHub: priyanshu73/outreach-pipeline      │
      └──────────────────────────┬─────────────────────────────┘
                                 │
                                 │ (Only when ready to deploy)
                                 │ railway up --detach
                                 ▼
      ┌────────────────────────────────────────────────────────┐
      │     Railway Production (Online Server)                 │
      │     SCHEDULER=true (Sends live emails)                 │
      └────────────────────────────────────────────────────────┘
```

1. **Local Development**:
   - Both developers make code changes locally and test using `docker compose up`.
   - Both developers share the same Supabase database, so new templates or imported leads are visible to everyone.
2. **Version Control via GitHub**:
   - Developer creates a branch: `git checkout -b feature/cool-feature`
   - Commits changes: `git commit -m "Add feature"`
   - Pushes branch: `git push origin feature/cool-feature`
   - Opens a Pull Request on GitHub for review before merging into `master`.
3. **Deploying to Railway**:
   - Pushing to GitHub **does not** touch Railway production automatically.
   - When a feature is ready for production, run:
     ```bash
     railway up --detach
     ```
   - Railway builds the Docker image in the cloud and deploys it live in ~60 seconds.

---

## 🛠️ Handy Docker Commands

| Action | Command |
|---|---|
| **Build & start** | `docker compose up --build` |
| **Start in background** | `docker compose up -d` |
| **View container logs** | `docker compose logs -f` |
| **Stop container** | `docker compose down` |
| **Run unit tests inside container** | `docker compose run --rm web python -m unittest discover tests` |
| **Rebuild without cache** | `docker compose build --no-cache` |
