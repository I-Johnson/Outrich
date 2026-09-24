# Outreach Admin

Private, single-admin lead discovery and cold-outreach application. It imports and standardizes CSVs, discovers business leads through SERP/crawling, manages deduplicated contacts in Supabase, and schedules personalized 1-to-1 outreach through Gmail and Pingram.

> 📖 **New to the team?** Check out the **[Developer Onboarding Guide (ONBOARDING.md)](ONBOARDING.md)** for 5-minute local Docker setup, shared Supabase configuration, and Git workflow.

---

## 🚀 Daily Development & Deployment Workflow

You can make all your code changes locally and deploy them directly to Railway without needing to run containers or complex local servers.

### The 3-Step Deploy Workflow

1. **Make code changes** in your editor (templates, routes, business logic, etc.).
2. **Commit changes locally** (tracked in your local Git):
   ```bash
   git add .
   git commit -m "Describe your changes"
   ```
3. **Deploy directly to Railway**:
   ```bash
   railway up
   ```
   * Railway uploads the updated code, builds the container in the cloud, and redeploys automatically in ~1 minute.
   * View live changes immediately at:
     👉 **[https://outreach-pipeline-production.up.railway.app](https://outreach-pipeline-production.up.railway.app)**

---

## ⚙️ How Environment Variables Work (Local vs. Railway)

> **Important Concept:** Does Railway automatically read your local `.env` file?
>
> **No.** Your local `.env` file is strictly kept on your machine (it is `.gitignore`d and `.railwayignore`d so your secrets are never exposed).
>
> Instead:
> * **Already Synced:** All your environment variables (Supabase URL, Service Role Key, Admin Login, Encryption Keys, Pingram, etc.) have **already been pushed to Railway's cloud settings**. Both environments point to the exact same live Supabase database.
> * **If you ever change or add a NEW variable locally:** You must also set it in Railway so the cloud deployment knows about it:
>   ```bash
>   railway variable set MY_NEW_KEY="value"
>   ```
>   *(Or update it via the Railway Web Dashboard under Settings → Variables)*.

---

## 🐳 Running with Docker (Recommended for Team)

For full details on environment configuration, team collaboration, and Docker commands, see the **[Docker & Environment Setup Guide](DOCKER_AND_ENV_GUIDE.md)**.

```bash
# 1. Copy the template and paste your keys
cp .env.example .env

# 2. Start the container with hot-reloading
docker compose up --build
```
Open **[http://localhost:8000](http://localhost:8000)**. It automatically mounts your `./app` directory for instant live code updates.

---

## 💻 Alternative: Running Locally with Python (No Docker Required)

If you prefer running without Docker:

```bash
# 1. Activate python environment
source .venv/bin/activate

# 2. Start local dev server with auto-reload
uvicorn app.main:app --reload --port 8000
```
Open [http://localhost:8000](http://localhost:8000). Both Docker and Python connect directly to the shared Supabase instance.

---

## 🗄️ Architecture & Backend

- **Runtime:** Dockerized FastAPI on Railway (single replica, owns APScheduler worker).
- **Database:** Supabase Postgres (accessed via REST API with server-side service-role key).
- **Queues:** Durable Postgres tables (`jobs`, `scrape_jobs`, `email_log`); no Redis required.
- **Drip Emailing:** Gmail SMTP with encrypted, independently branded sender accounts.
- **Safety Default:** `DRY_RUN=true` ensures no emails or credits are consumed until explicitly toggled.

### Gmail SMTP on Railway

Gmail SMTP works locally with an app password. Railway blocks outbound SMTP on
Free, Trial, and Hobby plans, which produces `OSError: [Errno 101] Network is
unreachable` before Gmail ever sees the login. This project therefore defaults
to direct SMTP locally and the authenticated Supabase `send-gmail` Edge Function
on Railway (`GMAIL_TRANSPORT=auto`). Gmail app passwords are encrypted before
storage and are sent to the authenticated function only over HTTPS.

As an alternative, a Railway Pro workspace can use direct Gmail SMTP instead of
the Supabase HTTPS bridge.

The Settings test-email action and campaign send log display the provider error.
Manual sends end in `failed` (with a retry button) rather than remaining in
`sending` or silently requeueing.

### Multiple Gmail senders

Add, edit, enable, or disable Gmail senders from **Settings → Gmail sender
accounts**. Each sender has its own email, encrypted app password, display name,
reply-to, and signature. Campaign creation can select any number of enabled
senders; durable email-log rows rotate between them and retain the assigned
sender for retries. Each account receives an independent send cursor, delay
jitter, and daily cap. The original `GMAIL_USER` / `GMAIL_USER_2` environment
variables remain as compatibility fallbacks only.

---

## 🛠️ Useful Commands

| Task | Command |
| :--- | :--- |
| **Deploy changes** | `railway up` |
| **View live logs** | `railway logs` |
| **Check service status** | `railway status` |
| **Set an env var** | `railway variable set KEY=VALUE` |
| **Run unit tests** | `.venv/bin/python -m unittest discover -s tests -v` |
| **Check git history** | `git log --oneline` |
