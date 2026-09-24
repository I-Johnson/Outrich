# 🚀 Developer Onboarding Guide

Welcome to the **Outreach Pipeline** team! This guide walks you through setting up your local environment, running the app inside Docker with live hot-reloading, understanding the shared database, and pushing changes safely through Git to production.

---

## 🏗️ Architecture Overview

The system is designed to be lightweight, modular, and fast:
- **Backend & Web**: FastAPI + Jinja2 templates (Python 3.12).
- **Local Runtime**: Docker Compose with volume-mounted live hot-reloading.
- **Database**: Shared Supabase Postgres (accessed over secure REST API via service-role key).
- **Outreach Channels**: Pingram API (domain conveyor belt with multi-rep rotation) + Gmail SMTP.
- **AI Engine**: Google Gemini Flash (`gemini-flash-latest`) for CSV column mapping, contact normalization, and template generation.
- **Production Hosting**: Railway (`https://outreach-pipeline-production.up.railway.app`).

---

## ⚡ 5-Minute Quickstart

### 1. Prerequisites
Ensure you have the following installed on your computer:
- [Git](https://git-scm.com/)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (macOS, Windows, or Linux)
- A GitHub account with collaborator access to [priyanshu73/outreach-pipeline](https://github.com/priyanshu73/outreach-pipeline).

---

### 2. Clone the Repository
```bash
git clone https://github.com/priyanshu73/outreach-pipeline.git
cd outreach-pipeline
```

---

### 3. Set Up Your Environment File (`.env`)

1. Copy the example configuration template:
   ```bash
   cp .env.example .env
   ```
2. Request the current `.env` secrets from your team lead (or paste the shared Supabase keys).
3. **Crucial Local Safety Settings**: In your local `.env`, always ensure:
   ```env
   # 1. Connect to the shared cloud database
   DATABASE_BACKEND=supabase
   SUPABASE_URL=https://yflxaxidedjpancbffln.supabase.co
   SUPABASE_SERVICE_ROLE_KEY=your-shared-key-here

   # 2. DISABLE background sending locally!
   # (Railway handles sending in production; you do not want your laptop dispatching emails)
   SCHEDULER_ENABLED=false
   DRY_RUN=true
   ```

> [!IMPORTANT]
> **Does local Docker open the same Supabase database?**
> **YES.** When `DATABASE_BACKEND=supabase` is set, your local Docker container queries the shared Supabase database directly over HTTPS. Any lead you upload, campaign you create, or template you save will be immediately visible on both your local machine and in Railway production.

---

### 4. Start the Application with Docker

Make sure Docker Desktop is open, then run:
```bash
docker compose up --build
```

- Docker will download the base Python image, install all requirements, and start the web server.
- Open your browser to: **[http://localhost:8000](http://localhost:8000)**
- Log in with the credentials defined in your `.env` (default: `ADMIN_EMAIL` and `ADMIN_PASSWORD`).

---

## 🔄 Daily Development & Live Hot-Reloading

You do **not** need to rebuild the Docker container when you edit code.

The `docker-compose.yml` mounts your local `./app` directory directly into the running container:
- Edit any Python route in `app/main.py` → Uvicorn automatically reloads in ~0.5 seconds.
- Edit any HTML template in `app/web/templates/` → Refresh your browser to see the updates immediately.

### Stopping and Restarting Docker
- **Stop**: Press `Ctrl + C` in the terminal, or run `docker compose down`.
- **Start in background**: `docker compose up -d`
- **View live logs**: `docker compose logs -f`

---

## 🧪 Running Automated Tests

Always run tests before committing code to make sure nothing is broken:

```bash
# Run tests inside the Docker container
docker compose run --rm web python -m unittest discover tests

# Or if you have Python 3.12+ installed locally
python -m unittest discover tests
```
*All 46 unit tests should complete in under 0.1 seconds.*

---

## 🌿 Git & Collaboration Workflow

### Step 1: Always Start from an Up-to-Date `master`
```bash
git checkout master
git pull origin master
```

### Step 2: Create a Feature Branch
```bash
git checkout -b feature/short-description
# Example: git checkout -b feature/filter-ui-upgrade
```

### Step 3: Write Code & Test Locally
- Check your changes at `http://localhost:8000`.
- Verify unit tests pass.

### Step 4: Commit Your Changes
```bash
git add .
git commit -m "Brief summary of what changed and why"
```

### Step 5: Push Branch to GitHub & Open a Pull Request
```bash
git push -u origin feature/short-description
```
1. Go to [https://github.com/priyanshu73/outreach-pipeline](https://github.com/priyanshu73/outreach-pipeline).
2. Click **"Compare & pull request"**.
3. Add a description of what you changed.
4. Once reviewed and approved, merge the pull request into `master`.

---

## 🚀 How Production Deployment Works

There are two ways code moves from GitHub to Railway production:

### Method A: Manual CLI Deploy (Current Setup)
Only developers who have been invited to the Railway project can deploy from the CLI:
1. Pull the merged `master` branch:
   ```bash
   git checkout master && git pull origin master
   ```
2. Deploy to Railway:
   ```bash
   railway up --detach
   ```
3. Railway builds the container in the cloud and updates the live site in ~60 seconds.

### Method B: Git-Triggered Auto-Deploy (Continuous Deployment)
When Railway is linked to GitHub (under Railway Settings → Source → Connect GitHub Repo):
- **Every time a pull request is merged into `master` on GitHub, Railway automatically detects the commit, builds the container, and deploys it live.**
- No terminal commands needed to deploy.

---

## 🧰 Adding New Dependencies

If you install a new Python package (e.g. `pandas` or `stripe`):
1. Add the package name to [`requirements.txt`](requirements.txt).
2. Rebuild your local Docker container:
   ```bash
   docker compose build --no-cache
   docker compose up
   ```
3. Commit `requirements.txt` to your git branch.

---

## ❓ Troubleshooting FAQ

### 1. "Port 8000 is already in use"
Another local server or previous Docker container is holding port 8000.
```bash
# Stop any running Docker compose instances
docker compose down

# On Mac/Linux, find what is using port 8000 and kill it
lsof -i :8000
kill -9 <PID>
```

### 2. "Database connection error or 401 Unauthorized"
Verify your `.env` has:
- `SUPABASE_URL=https://yflxaxidedjpancbffln.supabase.co`
- Valid `SUPABASE_SERVICE_ROLE_KEY` (service role key, not public anon key).

### 3. "I want an isolated scratchpad database without touching live leads"
Change one line in your local `.env`:
```env
DATABASE_BACKEND=sqlite
```
The app will instantly switch to a local file database at `data/outreach.db`.

---

### Need Help?
- Check logs: `docker compose logs -f`
- Railway dashboard: [https://railway.com/project/30d1596c-68bd-4181-93fb-2313dc31e7ac](https://railway.com/project/30d1596c-68bd-4181-93fb-2313dc31e7ac)
- Live Production App: [https://outreach-pipeline-production.up.railway.app](https://outreach-pipeline-production.up.railway.app)
