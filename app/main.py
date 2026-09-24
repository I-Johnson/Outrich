from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import csv
import io
import json
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.adapters import get_provider
from app.adapters.mapbox_geocoding import search_locations
from app.adapters.public_records import label_for_state
from app.config import settings as env
from app.core.ai_copywriter import generate_or_improve
from app.core.campaign_reporting import campaign_performance
from app.core.crypto import encrypt_secret
from app.core.gmail_senders import get_gmail_sender, list_gmail_senders, seed_legacy_gmail_senders, sender_context
from app.core.freight import evaluate_inbound, extract_offer, format_freight_message, freight_config, load_economics, parse_destinations, poll_freight_replies, seed_freight_settings, seed_freight_template, send_draft, send_first_touch, set_thread_state, verify_load_facts
from app.core.importer import FIELDS, build_preview, confirm_import, remap_preview, undo_import
from app.core.leads import duplicate_reason, normalize_email, normalize_phone, normalize_website, short_name, valid_email
from app.core.lead_status import delete_unused_client, set_client_status
from app.core.schedule import calendar_events
from app.core.sender import cancel_campaign_queue, delete_campaign, queue_campaign, refresh_campaign_states, send_due
from app.core.template_engine import render_template, unknown_variables
from app.db import count, init_db, new_id, now_iso, store
from app.jobs import scheduler

logging.basicConfig(level=logging.INFO)
ROOT = Path(__file__).resolve().parent.parent
app = FastAPI(title="Outreach Admin")
app.mount("/static", StaticFiles(directory=ROOT / "app" / "web" / "static"), name="static")
templates = Jinja2Templates(directory=str(ROOT / "app" / "web" / "templates"))
LOGIN_ATTEMPTS: dict[str, list[float]] = {}


def seed_templates():
    if store.list("email_templates", order="", limit=1): return
    for item in json.loads((ROOT / "seed" / "templates.json").read_text()):
        stamp = now_iso(); store.insert("email_templates", {"id": new_id(), **item, "active": True, "created_at": stamp, "updated_at": stamp})


@app.on_event("startup")
def startup():
    init_db(); seed_templates(); seed_freight_template(); seed_freight_settings(); seed_legacy_gmail_senders(); scheduler.start()


@app.middleware("http")
async def admin_auth(request: Request, call_next):
    public = request.url.path in {"/login", "/health"} or request.url.path.startswith("/static/") or request.url.path.startswith("/webhooks/")
    if not public and not request.session.get("admin"):
        return RedirectResponse(f"/login?next={request.url.path}", 303)
    response = await call_next(request)
    response.headers.update({"X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin"})
    return response


# Added after the auth middleware so session decoding wraps it.
app.add_middleware(SessionMiddleware, secret_key=env.SESSION_SECRET, same_site="lax", https_only=env.PUBLIC_BASE_URL.startswith("https://"))


def page(request: Request, name: str, **context):
    workspace = context.pop("workspace", "freight" if request.url.path.startswith("/freight") else "outreach")
    context.update({"request": request, "env": env, "app_settings": store.get("settings", 1) or {}, "workspace": workspace})
    return templates.TemplateResponse(request=request, name=name, context=context)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""): return page(request, "login.html", error=error)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    key = request.client.host if request.client else "unknown"; now = time.time()
    attempts = [x for x in LOGIN_ATTEMPTS.get(key, []) if now - x < 900]
    if len(attempts) >= 8: return RedirectResponse("/login?error=Too+many+attempts.+Try+again+later.", 303)
    if not secrets.compare_digest(email.lower().strip(), env.ADMIN_EMAIL) or not secrets.compare_digest(password, env.ADMIN_PASSWORD):
        attempts.append(now); LOGIN_ATTEMPTS[key] = attempts; return RedirectResponse("/login?error=Invalid+email+or+password", 303)
    LOGIN_ATTEMPTS.pop(key, None); request.session["admin"] = env.ADMIN_EMAIL; return RedirectResponse("/", 303)


@app.post("/logout")
def logout(request: Request): request.session.clear(); return RedirectResponse("/login", 303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    campaigns = store.list("campaigns", limit=100)
    logs = store.list("email_log", order="", limit=20000)
    recent = store.list("email_log", limit=10)
    cfg = store.get("settings", 1) or {}
    senders = list_gmail_senders(storage=store, cfg=cfg)
    accounts = {str(row["id"]): row.get("display_name") or row.get("email") or "Sender" for row in senders}

    # Calculate per-sender metrics for all senders
    today_utc = datetime.now(timezone.utc).date().isoformat()
    scheduled_by_acc = Counter()
    sent_by_acc = Counter()
    sent_today_by_acc = Counter()
    for log in logs:
        acc = str(log.get("sender_account") or "1")
        st = log.get("status")
        if st in {"queued", "sending"}:
            scheduled_by_acc[acc] += 1
        elif st in {"sent", "replied", "bounced"}:
            sent_by_acc[acc] += 1
            sent_at = log.get("sent_at")
            if sent_at and sent_at[:10] == today_utc:
                sent_today_by_acc[acc] += 1

    pingram_cap = int(cfg.get("pingram_daily_cap") or 30)
    gmail_cap = int(cfg.get("daily_cap") or 20)

    sender_stats = []
    for s in senders:
        sid = str(s["id"])
        provider = (s.get("provider") or "gmail").lower()
        cap = pingram_cap if provider == "pingram" else gmail_cap
        sig_first = (s.get("signature") or "").strip().split("\n")[0] if s.get("signature") else ""
        sender_stats.append({
            "id": sid,
            "display_name": s.get("display_name") or "Outreach",
            "email": s.get("email") or "",
            "provider": provider,
            "signature": sig_first,
            "daily_cap": cap,
            "active": bool(s.get("active", True)),
            "scheduled": scheduled_by_acc[sid],
            "sent": sent_by_acc[sid],
            "sent_today": sent_today_by_acc[sid],
            "utilization": round((sent_today_by_acc[sid] / cap) * 100) if cap else 0,
        })
    sender_stats.sort(key=lambda x: (x["provider"] != "pingram", -x["scheduled"], x["display_name"]))

    replies = count("clients", {"status": "replied"}) + count("clients", {"status": "demo_booked"})
    events = calendar_events(logs, campaigns, include_paused=True, include_provider=True)

    return page(
        request,
        "dashboard.html",
        stats={
            "leads": count("clients"),
            "queued": count("email_log", {"status": "queued"}),
            "sent": sum(1 for log in logs if log.get("sent_at")),
            "replies": replies,
            "bounced": count("clients", {"status": "bounced"}),
            "scrapes": count("scrape_jobs"),
        },
        campaigns=campaigns[:8],
        recent=recent,
        calendar_events=events,
        calendar_accounts=accounts,
        sender_stats=sender_stats,
        pingram_reps_count=sum(1 for s in sender_stats if s["provider"] == "pingram"),
        gmail_senders_count=sum(1 for s in sender_stats if s["provider"] != "pingram"),
        total_domain_cap=(sum(1 for s in sender_stats if s["provider"] == "pingram") * pingram_cap),
    )


def _optional_float(value) -> float | None:
    value = str(value or "").strip().replace(",", "")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _optional_int(value) -> int | None:
    number = _optional_float(value)
    return int(number) if number is not None else None


@app.get("/freight", response_class=HTMLResponse)
def freight_dashboard(request: Request, load_id: str = ""):
    loads = store.list("freight_loads", order="updated_at desc", limit=500)
    missions = store.list("freight_missions", order="created_at desc", limit=500)
    profiles = store.list("freight_truck_profiles", order="created_at desc", limit=500)
    mission_map = {str(row["id"]): row for row in missions}
    profile_map = {str(row["id"]): row for row in profiles}
    threads = store.list("freight_threads", order="updated_at desc", limit=1000)
    thread_map = {str(row["load_id"]): row for row in threads}
    open_alerts = store.list("freight_alerts", {"status": "open"}, order="created_at desc", limit=200)
    alerts_by_thread: dict[str, list[dict]] = {}
    for alert in open_alerts:
        alerts_by_thread.setdefault(str(alert.get("thread_id")), []).append(alert)
    pending_drafts = store.list("freight_drafts", {"status": "pending"}, order="created_at desc", limit=500)
    drafts_by_thread = {str(row["thread_id"]): row for row in pending_drafts}
    for load in loads:
        mission = mission_map.get(str(load.get("mission_id"))) or {}
        profile_id = load.get("truck_profile_id") or mission.get("truck_profile_id")
        thread = thread_map.get(str(load["id"])) or {}
        load["mission"] = mission
        load["profile"] = profile_map.get(str(profile_id)) or {}
        load["thread"] = thread
        load["economics"] = load_economics(load)
        load["alerts"] = alerts_by_thread.get(str(thread.get("id")), [])
        load["draft"] = drafts_by_thread.get(str(thread.get("id")))
    selected = next((row for row in loads if str(row["id"]) == str(load_id)), None) or (loads[0] if loads else None)
    messages = []
    if selected and selected.get("thread"):
        raw_messages = store.list("freight_messages", {"thread_id": selected["thread"]["id"]}, order="created_at asc", limit=500)
        messages = [format_freight_message(dict(m)) for m in raw_messages]
    cfg = store.get("settings", 1) or {}
    freight_settings = store.get("freight_settings", 1) or {}
    gmail_senders = [row for row in list_gmail_senders(active_only=True, storage=store, cfg=cfg) if row.get("provider", "gmail") == "gmail"]
    freight_templates = store.list("email_templates", {"active": True, "vertical": "freight"}, order="created_at asc", limit=200)
    default_template = next((t for t in freight_templates if str(t["id"]) == str(freight_settings.get("default_template_id"))), None) or (freight_templates[0] if freight_templates else None)
    cursors = store.list("freight_mail_cursors", order="updated_at desc", limit=100)
    return page(
        request, "freight.html", workspace="freight", loads=loads, selected_load=selected,
        messages=messages, missions=missions, profiles=profiles, freight_templates=freight_templates,
        default_template=default_template,
        gmail_senders=gmail_senders, freight_settings=freight_settings, open_alerts=open_alerts, mail_cursors=cursors,
    )


@app.get("/freight/missions", response_class=HTMLResponse)
def freight_missions(request: Request, mission_id: str = "", profile_id: str = "", tab: str = ""):
    missions = store.list("freight_missions", order="created_at desc", limit=500)
    profiles = store.list("freight_truck_profiles", order="created_at desc", limit=500)
    fsettings = store.get("freight_settings", 1) or {}
    active_tab = tab or ("trucks" if profile_id else "missions")
    return page(
        request, "freight_missions.html", workspace="freight", missions=missions, profiles=profiles,
        freight_settings=fsettings, active_tab=active_tab,
        edit_mission=store.get("freight_missions", mission_id) if mission_id else None,
        edit_profile=store.get("freight_truck_profiles", profile_id) if profile_id else None,
    )


@app.get("/freight/settings", response_class=HTMLResponse)
def freight_settings_page(request: Request, tab: str = "identity", edit_template: str = ""):
    cfg = store.get("settings", 1) or {}
    senders = [row for row in list_gmail_senders(active_only=True, storage=store, cfg=cfg) if row.get("provider", "gmail") == "gmail"]
    freight_templates = store.list("email_templates", {"active": True, "vertical": "freight"}, order="created_at asc", limit=200)
    fsettings = store.get("freight_settings", 1) or {}
    default_template = next((t for t in freight_templates if str(t["id"]) == str(fsettings.get("default_template_id"))), None) or (freight_templates[0] if freight_templates else None)
    edit_tpl = store.get("email_templates", edit_template) if edit_template else None
    missions = store.list("freight_missions", order="created_at desc", limit=500)
    profiles = store.list("freight_truck_profiles", order="created_at desc", limit=500)
    return page(
        request, "freight_settings.html", workspace="freight",
        freight_settings=fsettings, gmail_senders=senders,
        freight_templates=freight_templates, default_template=default_template,
        active_tab=tab, edit_template=edit_tpl,
        missions=missions, profiles=profiles,
    )


@app.post("/freight/settings")
async def freight_settings_save(request: Request):
    form = await request.form()
    sender_ids = {str(row["id"]) for row in list_gmail_senders(active_only=True, storage=store, cfg=store.get("settings", 1) or {}) if row.get("provider", "gmail") == "gmail"}
    template_ids = {str(row["id"]) for row in store.list("email_templates", {"active": True, "vertical": "freight"}, order="", limit=200)}
    sender_id = str(form.get("default_sender_account") or "")
    template_id = str(form.get("default_template_id") or "")
    if sender_id not in sender_ids or template_id not in template_ids:
        return RedirectResponse("/freight/settings?notice=Choose+a+valid+Gmail+sender+and+Freight+template", 303)
    values = {
        "sender_name": str(form.get("sender_name") or "").strip() or "Freight Dispatch",
        "email_signature": str(form.get("email_signature") or "").strip() or "Freight Dispatch",
        "reply_to": normalize_email(str(form.get("reply_to") or "")),
        "default_sender_account": sender_id,
        "default_template_id": template_id,
        "updated_at": now_iso(),
    }
    store.update("freight_settings", 1, values)
    return RedirectResponse("/freight/settings?notice=Freight+settings+saved", 303)


@app.post("/freight/senders/add")
async def freight_sender_add(request: Request):
    form = await request.form()
    email = normalize_email(str(form.get("email") or ""))
    password = str(form.get("app_password") or "").replace(" ", "").strip()
    display_name = str(form.get("display_name") or "").strip()
    reply_to = normalize_email(str(form.get("reply_to") or ""))
    auto_select = form.get("auto_select", "on") in {"on", "true", "1", "yes"}

    if not valid_email(email):
        return RedirectResponse("/freight/settings?notice=Valid+Gmail+address+is+required", 303)
    if not password:
        return RedirectResponse("/freight/settings?notice=Google+App+Password+is+required", 303)

    existing = [row for row in list_gmail_senders(storage=store) if str(row.get("email") or "").lower() == email]
    stamp = now_iso()
    if existing:
        sender_id = str(existing[0]["id"])
        store.update("gmail_senders", sender_id, {
            "app_password_encrypted": encrypt_secret(password),
            "display_name": display_name or existing[0].get("display_name") or "Freight Dispatch",
            "reply_to": reply_to or existing[0].get("reply_to") or email,
            "active": True,
            "updated_at": stamp,
        })
    else:
        sender_id = new_id()
        store.insert("gmail_senders", {
            "id": sender_id,
            "email": email,
            "display_name": display_name or "Freight Dispatch",
            "signature": "",
            "reply_to": reply_to or email,
            "app_password_encrypted": encrypt_secret(password),
            "active": True,
            "provider": "gmail",
            "created_at": stamp,
            "updated_at": stamp,
        })

    if auto_select:
        fsettings = store.get("freight_settings", 1) or {}
        store.update("freight_settings", 1, {
            "default_sender_account": sender_id,
            "sender_name": display_name or fsettings.get("sender_name") or "Freight Dispatch",
            "reply_to": reply_to or fsettings.get("reply_to") or email,
            "updated_at": stamp,
        })

    return RedirectResponse("/freight/settings?notice=Email+account+connected+and+selected+for+Freight", 303)


@app.post("/freight/profiles/save")
async def freight_profile_save(request: Request):
    form = await request.form()
    row_id = str(form.get("id") or "")
    stamp = now_iso()
    shareable = [str(value) for value in form.getlist("shareable_fields")]
    data = {
        "name": str(form.get("name") or "").strip(),
        "current_city": str(form.get("current_city") or "").strip(),
        "current_state": str(form.get("current_state") or "").strip().upper(),
        "equipment_type": str(form.get("equipment_type") or "").strip(),
        "trailer_length_ft": _optional_int(form.get("trailer_length_ft")),
        "max_weight_lbs": _optional_int(form.get("max_weight_lbs")),
        "team_status": str(form.get("team_status") or "").strip(),
        "mc_number": str(form.get("mc_number") or "").strip(),
        "dot_number": str(form.get("dot_number") or "").strip(),
        "dispatcher_name": str(form.get("dispatcher_name") or "").strip(),
        "dispatcher_phone": str(form.get("dispatcher_phone") or "").strip(),
        "shareable_fields": shareable,
        "active": bool(form.get("active")),
        "updated_at": stamp,
    }
    if not data["name"]:
        return RedirectResponse("/freight/missions?tab=trucks&notice=Truck+profile+name+is+required", 303)
    if row_id:
        store.update("freight_truck_profiles", row_id, data)
    else:
        row_id = new_id(); store.insert("freight_truck_profiles", {"id": row_id, **data, "created_at": stamp})
    return RedirectResponse(f"/freight/missions?tab=trucks&profile_id={row_id}&notice=Truck+profile+saved", 303)


@app.post("/freight/missions/save")
async def freight_mission_save(request: Request):
    form = await request.form()
    row_id = str(form.get("id") or "")
    stamp = now_iso()
    destinations = parse_destinations(form.getlist("destination_label"), form.getlist("destination_kind"), form.getlist("destination_radius"))
    execution_mode = str(form.get("execution_mode") or form.get("mode") or "").strip().lower()
    if execution_mode == "auto":
        permissions = {
            "auto_profile_reply": True,
            "auto_counter": True,
            "auto_pass": True,
        }
    elif execution_mode == "approve":
        permissions = {
            "auto_profile_reply": False,
            "auto_counter": False,
            "auto_pass": False,
        }
    else:
        permissions = {
            "auto_profile_reply": bool(form.get("auto_profile_reply")),
            "auto_counter": bool(form.get("auto_counter")),
            "auto_pass": bool(form.get("auto_pass")),
        }
    data = {
        "name": str(form.get("name") or "").strip(),
        "truck_profile_id": str(form.get("truck_profile_id") or "") or None,
        "origin_city": str(form.get("origin_city") or "").strip(),
        "origin_state": str(form.get("origin_state") or "").strip().upper(),
        "origin_deadhead_miles": _optional_int(form.get("origin_deadhead_miles")) or 0,
        "pickup_start": str(form.get("pickup_start") or "") or None,
        "pickup_end": str(form.get("pickup_end") or "") or None,
        "equipment_type": str(form.get("equipment_type") or "").strip(),
        "trailer_length_ft": _optional_int(form.get("trailer_length_ft")),
        "max_weight_lbs": _optional_int(form.get("max_weight_lbs")),
        "destinations": destinations,
        "floor_total": _optional_float(form.get("floor_total")),
        "target_total": _optional_float(form.get("target_total")),
        "floor_loaded_rpm": _optional_float(form.get("floor_loaded_rpm")),
        "floor_all_in_rpm": _optional_float(form.get("floor_all_in_rpm")),
        "target_all_in_rpm": _optional_float(form.get("target_all_in_rpm")),
        "counter_amount": _optional_float(form.get("counter_amount")),
        "maximum_counter_rounds": _optional_int(form.get("maximum_counter_rounds")) or 3,
        "permissions": permissions,
        "active": bool(form.get("active")),
        "updated_at": stamp,
    }
    if not data["name"] or not destinations:
        return RedirectResponse("/freight/missions?tab=missions&notice=Mission+name+and+at+least+one+destination+are+required", 303)
    if row_id:
        store.update("freight_missions", row_id, data)
    else:
        row_id = new_id(); store.insert("freight_missions", {"id": row_id, **data, "created_at": stamp})
    return RedirectResponse(f"/freight/missions?tab=missions&mission_id={row_id}&notice=Mission+saved", 303)


@app.post("/freight/loads/send")
async def freight_load_send(request: Request):
    form = await request.form()
    mission_id = str(form.get("mission_id") or "")
    mission = store.get("freight_missions", mission_id)
    if not mission:
        return RedirectResponse("/freight?notice=Choose+a+mission", 303)
    profile_id = str(form.get("truck_profile_id") or mission.get("truck_profile_id") or "")
    profile = store.get("freight_truck_profiles", profile_id) if profile_id else None
    if not profile:
        return RedirectResponse("/freight?notice=Choose+a+truck+profile", 303)
    freight_settings = store.get("freight_settings", 1) or {}
    sender_account = str(freight_settings.get("default_sender_account") or "")
    active_senders = [s for s in list_gmail_senders(active_only=True, storage=store, cfg=store.get("settings", 1) or {}) if s.get("provider", "gmail") == "gmail"]
    if not sender_account or not any(str(s["id"]) == sender_account for s in active_senders):
        if active_senders:
            sender_account = str(active_senders[0]["id"])
    template_id = str(freight_settings.get("default_template_id") or "")
    freight_templates = store.list("email_templates", {"active": True, "vertical": "freight"}, order="created_at asc", limit=100)
    if not template_id or not any(str(t["id"]) == template_id for t in freight_templates):
        if freight_templates:
            template_id = str(freight_templates[0]["id"])
    if not sender_account or not template_id:
        return RedirectResponse("/freight/settings?notice=Finish+Freight+email+settings+before+sending", 303)
    origin_city = str(mission.get("origin_city") or profile.get("current_city") or "").strip()
    origin_state = str(mission.get("origin_state") or profile.get("current_state") or "").strip().upper()
    stamp = now_iso(); load_id = new_id()
    row = {
        "id": load_id, "mission_id": mission_id,
        "truck_profile_id": profile_id,
        "broker_email": normalize_email(str(form.get("broker_email") or "")),
        "broker_company": "",
        "origin_city": origin_city, "origin_state": origin_state,
        "destination_city": "Open destinations", "destination_state": "",
        "pickup_date": mission.get("pickup_start") or None,
        "loaded_miles": None, "deadhead_miles": None, "posted_rate": None, "current_offer": None,
        "dat_reference": "",
        "equipment_type": str(mission.get("equipment_type") or profile.get("equipment_type") or "").strip(),
        "template_id": template_id,
        "sender_account": sender_account,
        "subject": str(form.get("subject") or "").strip() or "Load inquiry", "status": "draft", "current_round": 0,
        "created_at": stamp, "updated_at": stamp,
    }
    if not valid_email(row["broker_email"]):
        return RedirectResponse("/freight?notice=Enter+a+valid+broker+email", 303)
    store.insert("freight_loads", row)
    try:
        send_first_touch(load_id)
    except Exception as exc:
        return RedirectResponse(f"/freight?load_id={load_id}&notice=Send+failed:+{quote(str(exc)[:160])}", 303)
    return RedirectResponse(f"/freight?load_id={load_id}&notice=Load+email+sent", 303)


@app.post("/freight/drafts/{draft_id}/send")
async def freight_draft_send(request: Request, draft_id: str):
    draft = store.get("freight_drafts", draft_id)
    if not draft:
        raise HTTPException(404, "Draft not found")
    thread = store.get("freight_threads", draft["thread_id"]) or {}
    form = await request.form()
    body_text = str(form.get("body_text") or draft.get("body_text") or "").strip()
    if not body_text:
        return RedirectResponse(f"/freight?load_id={thread.get('load_id','')}&notice=Reply+cannot+be+empty", 303)
    store.update("freight_drafts", draft_id, {"body_text": body_text, "updated_at": now_iso()})
    try:
        send_draft(draft_id)
    except ValueError as exc:
        return RedirectResponse(f"/freight?load_id={thread.get('load_id','')}&notice=Send+failed:+{quote(str(exc)[:160])}", 303)
    return RedirectResponse(f"/freight?load_id={thread.get('load_id','')}&notice=Reply+sent", 303)


@app.post("/freight/alerts/{alert_id}/resolve")
def freight_alert_resolve(alert_id: str):
    alert = store.get("freight_alerts", alert_id)
    if not alert:
        raise HTTPException(404, "Alert not found")
    thread = store.get("freight_threads", alert["thread_id"]) or {}
    store.update("freight_alerts", alert_id, {"status": "resolved", "resolved_at": now_iso()})
    return RedirectResponse(f"/freight?load_id={thread.get('load_id','')}&notice=Alert+resolved", 303)


@app.post("/freight/threads/{thread_id}/state")
async def freight_thread_state(thread_id: str, request: Request):
    form = await request.form()
    try:
        result = set_thread_state(thread_id, str(form.get("state") or ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(f"/freight?load_id={result['load']['id']}&notice=Load+marked+{quote(result['thread']['state'])}", 303)


@app.post("/freight/loads/{load_id}/facts")
async def freight_load_facts(load_id: str, request: Request):
    form = await request.form()
    try:
        verify_load_facts(load_id, dict(form))
        # After facts are verified, resolve open fact alerts and re-evaluate inbound message if present
        thread_rows = store.list("freight_threads", {"load_id": load_id}, order="", limit=1)
        if thread_rows:
            th = thread_rows[0]
            for al in store.list("freight_alerts", {"thread_id": th["id"], "status": "open"}):
                if al.get("kind") in {"miles_unverified", "conflicting_load_facts", "verify_load_facts", "ambiguous_rate"}:
                    store.update("freight_alerts", al["id"], {"status": "resolved", "resolved_at": now_iso()})
            inbound_msgs = store.list("freight_messages", {"thread_id": th["id"], "direction": "in"}, order="created_at desc", limit=1)
            if inbound_msgs:
                set_thread_state(th["id"], "waiting")
                evaluate_inbound(th, inbound_msgs[0], storage=store)
    except ValueError as exc:
        return RedirectResponse(f"/freight?load_id={load_id}&notice={quote(str(exc))}", 303)
    return RedirectResponse(f"/freight?load_id={load_id}&notice=Load+facts+verified+and+re-evaluated", 303)


@app.post("/freight/threads/{thread_id}/reply")
async def freight_thread_reply(thread_id: str, request: Request):
    thread = store.get("freight_threads", thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    form = await request.form()
    body_text = str(form.get("body_text") or "").strip()
    action_type = str(form.get("action_type") or "manual_reply")
    if not body_text:
        return RedirectResponse(f"/freight?load_id={thread.get('load_id','')}&notice=Reply+cannot+be+empty", 303)

    draft_id = new_id()
    reason = "counter_manual" if extract_offer(body_text) is not None else action_type
    store.insert("freight_drafts", {
        "id": draft_id,
        "thread_id": thread_id,
        "subject": thread.get("subject", ""),
        "body_text": body_text,
        "reason": reason,
        "policy_snapshot": {"manual": True},
        "in_reply_to_message_id": thread.get("last_message_id") or "",
        "status": "pending",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    })
    if thread.get("state") in {"needs_attention", "mismatch", "protected_review", "offer_review", "closed"}:
        set_thread_state(thread_id, "negotiating")
    try:
        send_draft(draft_id)
    except Exception as exc:
        return RedirectResponse(f"/freight?load_id={thread.get('load_id','')}&notice=Send+failed:+{quote(str(exc)[:160])}", 303)
    return RedirectResponse(f"/freight?load_id={thread.get('load_id','')}&notice=Reply+sent+to+broker", 303)



@app.post("/freight/check-mail")
def freight_check_mail():
    result = poll_freight_replies()
    return RedirectResponse(f"/freight?notice=Checked+mail:+{result['matched']}+matched+replies", 303)


@app.get("/leads", response_class=HTMLResponse)
def leads(request: Request, category: str = "", state: str = "", city: str = "", status: str = "", source: str = "", search: str = ""):
    all_clients = store.list("clients", limit=20000)
    total_leads_count = len(all_clients)
    
    status_counts = Counter(c.get("status") or "new" for c in all_clients)
    status_counts["all"] = total_leads_count

    categories = sorted({c.get("category") for c in all_clients if c.get("category")})[:50]
    states = sorted({c.get("state") for c in all_clients if c.get("state")})[:50]

    filters = {k: v for k, v in {"category": category, "state": state, "city": city, "status": status, "source": source}.items() if v}
    rows = store.list("clients", filters, limit=2000)
    if search:
        needle = search.lower()
        rows = [r for r in rows if needle in " ".join(str(r.get(k, "")) for k in ("business_name", "short_name", "owner_first_name", "email", "city", "phone", "category")).lower()]
    
    eligible_campaigns = [
        campaign for campaign in store.list("campaigns", limit=100)
        if campaign.get("state") in {"draft", "running", "paused"}
    ]
    return page(
        request,
        "leads.html",
        leads=rows,
        filters={**filters, "search": search},
        status_counts=status_counts,
        total_leads_count=total_leads_count,
        filtered_count=len(rows),
        categories=categories,
        states=states,
        batches=store.list("import_batches", limit=20),
        eligible_campaigns=eligible_campaigns,
    )


@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(request: Request, lead_id: str):
    lead = store.get("clients", lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    logs = store.list("email_log", {"client_id": lead_id}, order="created_at desc", limit=1000)
    campaigns = {str(row["id"]): row for row in store.list("campaigns", limit=1000)}
    templates_by_id = {str(row["id"]): row for row in store.list("email_templates", limit=1000)}
    for log in logs:
        log["campaign"] = campaigns.get(str(log.get("campaign_id"))) or {}
        log["template"] = templates_by_id.get(str(log.get("template_id"))) or {}
    return page(request, "lead_detail.html", lead=lead, logs=logs)


@app.post("/leads/save")
async def lead_save(request: Request):
    form = await request.form(); row_id = str(form.get("id") or ""); website, domain = normalize_website(str(form.get("website") or ""))
    data = {"business_name": str(form.get("business_name") or "").strip(), "owner_first_name": str(form.get("owner_first_name") or "").strip(),
            "category": str(form.get("category") or "").strip(), "city": str(form.get("city") or "").strip(), "state": str(form.get("state") or "").strip().upper(),
            "zip": str(form.get("zip") or "").strip(), "website": website, "domain": domain, "email": normalize_email(str(form.get("email") or "")),
            "phone": normalize_phone(str(form.get("phone") or "")), "outreach_angle": str(form.get("outreach_angle") or "").strip(), "notes": str(form.get("notes") or "").strip()}
    data["short_name"] = str(form.get("short_name") or "").strip() or short_name(data["business_name"]); data["updated_at"] = now_iso()
    if data["email"] and not valid_email(data["email"]): raise HTTPException(422, "Invalid email address")
    others = [x for x in store.list("clients", order="", limit=10000) if x["id"] != row_id]; reason = duplicate_reason(data, others)
    if reason: raise HTTPException(409, reason)
    if row_id:
        if not store.get("clients", row_id): raise HTTPException(404, "Lead not found")
        store.update("clients", row_id, data)
    else: store.insert("clients", {"id": new_id(), **data, "source": "manual", "extra": {}, "status": "new", "created_at": now_iso()})
    return RedirectResponse(f"/leads/{row_id}?notice=Lead+saved" if row_id else "/leads?notice=Lead+added", 303)


@app.post("/leads/{lead_id}/status")
def lead_status(lead_id: str, status: str = Form(...)):
    try:
        set_client_status(lead_id, status, storage=store)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    refresh_campaign_states()
    return RedirectResponse("/leads", 303)


@app.post("/leads/bulk")
async def leads_bulk(request: Request):
    form = await request.form(); ids = form.getlist("lead_ids"); action = str(form.get("action") or "")
    if not ids:
        return RedirectResponse("/leads?notice=Select+at+least+one+lead", 303)
    if action == "delete":
        deleted = protected = 0
        for lead_id in ids:
            try:
                deleted += int(delete_unused_client(str(lead_id), storage=store))
            except ValueError:
                protected += 1
        return RedirectResponse(f"/leads?notice=Deleted+{deleted}+unused+leads.+Protected+{protected}+with+email+history", 303)
    elif action.startswith("status:"):
        status = action.split(":", 1)[1]
        try:
            for lead_id in ids:
                set_client_status(lead_id, status, storage=store)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        refresh_campaign_states()
    elif action.startswith("campaign:"):
        campaign_id = action.split(":", 1)[1]
        campaign = store.get("campaigns", campaign_id)
        if not campaign or campaign.get("state") not in {"draft", "running", "paused"}:
            return RedirectResponse("/leads?notice=Choose+an+active+or+draft+campaign", 303)
        try:
            result = queue_campaign(campaign_id, {str(value) for value in ids})
        except ValueError as exc:
            return RedirectResponse(f"/leads?notice={quote(str(exc))}", 303)
        return RedirectResponse(f"/campaigns/{campaign_id}?notice=Queued+{result['queued']}+selected+leads%2C+skipped+{result['skipped']}", 303)
    return RedirectResponse("/leads", 303)


@app.post("/leads/{lead_id}/delete")
def lead_delete(lead_id: str):
    try:
        deleted = delete_unused_client(lead_id, storage=store)
    except ValueError as exc:
        return RedirectResponse(f"/leads/{lead_id}?notice={quote(str(exc))}", 303)
    if not deleted:
        raise HTTPException(404, "Lead not found")
    return RedirectResponse("/leads?notice=Unused+lead+deleted", 303)


@app.post("/imports/preview", response_class=HTMLResponse)
async def import_preview(request: Request, file: UploadFile = File(...)):
    try:
        content = await file.read()
        preview = build_preview(content, file.filename or "upload.csv")
        return page(request, "import_preview.html", batch=preview, fields=FIELDS)
    except Exception as exc:
        logging.getLogger(__name__).exception("CSV preview failed: %s", exc)
        return RedirectResponse(f"/leads?notice=Import+failed:+{quote(str(exc)[:120])}", 303)


@app.get("/imports/{batch_id}/preview", response_class=HTMLResponse)
def import_preview_view(request: Request, batch_id: str):
    batch = store.get("import_batches", batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")
    if batch.get("status") != "preview":
        return RedirectResponse(f"/leads?notice=Batch+is+already+{batch.get('status')}", 303)
    payload = json.loads(batch.get("rejected_csv") or "{}")
    ready = payload.get("ready", [])
    rejected = payload.get("rejected", [])
    headers = payload.get("headers", [])
    batch_data = {
        **batch,
        "ready": ready[:20],
        "ready_count": len(ready),
        "rejected": rejected[:20],
        "headers": headers,
    }
    return page(request, "import_preview.html", batch=batch_data, fields=FIELDS)


@app.post("/imports/{batch_id}/delete")
def import_delete(batch_id: str):
    batch = store.get("import_batches", batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")
    if batch.get("status") == "done":
        raise HTTPException(400, "Completed imports cannot be deleted directly; use undo instead.")
    store.delete("import_batches", {"id": batch_id})
    return RedirectResponse("/leads?notice=Import+preview+discarded", 303)


@app.post("/imports/{batch_id}/confirm")
async def import_confirm(request: Request, batch_id: str):
    form = await request.form()
    action = str(form.get("action") or "import")
    try:
        result = confirm_import(batch_id)
    except Exception as exc:
        logging.getLogger(__name__).exception("Confirm import failed: %s", exc)
        return RedirectResponse(f"/leads?notice=Import+confirmation+failed:+{quote(str(exc)[:120])}", 303)
    if action == "campaign":
        return RedirectResponse(f"/campaigns?batch_id={batch_id}&notice=Imported+{result['added']}+leads.+Create+your+campaign+now!", 303)
    return RedirectResponse(f"/leads?notice=Imported+{result['added']}+leads", 303)


@app.post("/imports/{batch_id}/remap", response_class=HTMLResponse)
async def import_remap(request: Request, batch_id: str):
    form = await request.form()
    mapping = {key.removeprefix("map__"): str(value) for key, value in form.items() if key.startswith("map__") and value}
    try:
        preview = remap_preview(batch_id, mapping)
        return page(request, "import_preview.html", batch=preview, fields=FIELDS)
    except Exception as exc:
        logging.getLogger(__name__).exception("Remap preview failed: %s", exc)
        return RedirectResponse(f"/leads?notice=Remap+failed:+{quote(str(exc)[:120])}", 303)


@app.post("/imports/{batch_id}/undo")
def import_undo(batch_id: str):
    try:
        deleted = undo_import(batch_id)
    except ValueError as exc:
        return RedirectResponse(f"/leads?notice={quote(str(exc))}", 303)
    return RedirectResponse(f"/leads?notice=Import+undone.+Deleted+{deleted}+unused+leads", 303)


@app.get("/imports/{batch_id}/rejected.csv")
def rejected_csv(batch_id: str):
    batch = store.get("import_batches", batch_id)
    if not batch: raise HTTPException(404)
    return PlainTextResponse(batch.get("rejected_csv") or "", media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=rejected-{batch_id}.csv"})


@app.get("/templates", response_class=HTMLResponse)
def template_list(request: Request, edit: str = "", vertical: str = "outreach"):
    if vertical == "freight":
        param = f"&edit_template={edit}" if edit else ""
        return RedirectResponse(f"/freight/settings?tab=templates{param}", 303)
    row = store.get("email_templates", edit) if edit else None
    templates = sorted(store.list("email_templates", {"vertical": vertical}), key=lambda x: (
        0 if (x.get("name") or "").startswith("HVAC") else (
            1 if (x.get("name") or "").startswith("Remodelers") else 2
        ),
        (x.get("name") or "").lower()
    ))
    return page(request, "templates.html", templates=templates, edit=row, vertical=vertical, workspace=vertical)


@app.post("/templates/save")
async def template_save(request: Request):
    form = await request.form(); row_id = str(form.get("id") or "")
    vertical = "freight" if form.get("vertical") == "freight" else "outreach"
    data = {"name": str(form.get("name") or ""), "subject": str(form.get("subject") or ""), "body": str(form.get("body") or ""), "type": str(form.get("type") or "plain"), "angle_tag": str(form.get("angle_tag") or ""), "active": bool(form.get("active")), "vertical": vertical, "updated_at": now_iso()}
    unknown = unknown_variables(data["subject"] + data["body"])
    if unknown:
        if vertical == "freight":
            return RedirectResponse(f"/freight/settings?tab=templates&edit_template={row_id or 'new'}&notice=Error: Unknown variables: {', '.join(unknown)}", 303)
        return RedirectResponse(f"/templates?vertical={vertical}&edit={row_id or 'new'}&notice=Error: Unknown variables: {', '.join(unknown)}", 303)
    if row_id: store.update("email_templates", row_id, data)
    else: store.insert("email_templates", {"id": new_id(), **data, "created_at": now_iso()})
    if vertical == "freight":
        return RedirectResponse("/freight/settings?tab=templates&notice=Template+saved+successfully", 303)
    return RedirectResponse(f"/templates?vertical={vertical}&notice=Template+saved+successfully", 303)


@app.post("/templates/ai-generate")
async def templates_ai_generate(request: Request):
    try: body = await request.json()
    except Exception: form = await request.form(); body = dict(form)
    mode = str(body.get("mode") or "generate")
    instructions = str(body.get("instructions") or "").strip()
    existing_subject = str(body.get("existing_subject") or "")
    existing_body = str(body.get("existing_body") or "")
    existing_name = str(body.get("existing_name") or "")
    existing_angle_tag = str(body.get("existing_angle_tag") or "")
    cfg = store.get("settings", 1) or {}
    try:
        result = generate_or_improve(
            mode=mode,
            instructions=instructions,
            business_context=cfg,
            existing_subject=existing_subject,
            existing_body=existing_body,
            existing_name=existing_name,
            existing_angle_tag=existing_angle_tag,
        )
        return JSONResponse({"ok": True, **result})
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Unexpected error: {exc}"}, status_code=500)


@app.api_route("/templates/{template_id}/preview", methods=["GET", "POST"], response_class=HTMLResponse)
def template_preview(request: Request, template_id: str):
    item = store.get("email_templates", template_id)
    if not item:
        raise HTTPException(404, "Template not found")
    vertical = item.get("vertical") or "outreach"
    cfg = freight_config(store) if vertical == "freight" else (store.get("settings", 1) or {})
    sig = cfg.get("email_signature") or ("Freight Dispatch" if vertical == "freight" else "")
    sample = ({"origin": "Phoenix, AZ", "destination": "Dallas, TX", "pickup_date": "tomorrow", "equipment": "53 ft dry van", "truck_location": "Phoenix, AZ", "mc_number": "123456", "dot_number": "987654", "broker_company": "Sample Broker", "loaded_miles": 1060, "deadhead_miles": 25, "posted_rate": "$2,650", "all_in_rpm": "2.44", "signature": sig} if vertical == "freight" else (store.list("clients", limit=1) or [{"short_name": "Acme Roofing", "category": "roofing", "city": "Austin", "state": "TX"}])[0])
    subject, sm = render_template(item["subject"], sample, cfg); body, bm = render_template(item["body"], sample, cfg)
    return page(request, "template_preview.html", template=item, subject=subject, body=body, missing=sorted(set(sm + bm)), vertical=vertical, workspace=vertical)


@app.get("/campaigns", response_class=HTMLResponse)
def campaigns(request: Request, edit: str = "", batch_id: str = ""):
    batch = store.get("import_batches", batch_id) if batch_id else None
    batches = store.list("import_batches", {"status": "done"}, order="uploaded_at desc", limit=10)
    cfg = store.get("settings", 1) or {}
    gmail_accounts = list_gmail_senders(active_only=True, storage=store, cfg=cfg)
    return page(request, "campaigns.html", campaigns=store.list("campaigns"), templates=store.list("email_templates", {"active": True, "vertical": "outreach"}), campaign=store.get("campaigns", edit) if edit else None, selected_batch=batch, batches=batches, batch_id=batch_id, gmail_accounts=gmail_accounts)


@app.post("/campaigns/save")
async def campaign_save(request: Request):
    form = await request.form(); cfg = store.get("settings", 1) or {}; stamp = now_iso()
    target = {k: str(form.get(k) or "").strip() for k in ("category", "state", "city", "status", "source", "import_batch_id") if form.get(k)}
    provider = str(form.get("provider") or "gmail").strip().lower()
    
    available_accounts = {str(row["id"]) for row in list_gmail_senders(active_only=True, storage=store, cfg=cfg) if row.get("provider", "gmail") == provider}
    gmail_accounts = [x for x in form.getlist("gmail_accounts") if x in available_accounts]
    if not gmail_accounts:
        return RedirectResponse("/settings?notice=Select+at+least+one+sender+identity", 303)
            
    data = {"name": str(form.get("name") or "Untitled campaign"), "target_filter": target, "template_ids": form.getlist("template_ids"), "provider": provider, "gmail_accounts": gmail_accounts, "daily_cap": int(cfg.get("daily_cap") or 20), "send_window": {"timezone": cfg.get("timezone"), "send_days": cfg.get("send_days"), "send_start": cfg.get("send_start"), "send_end": cfg.get("send_end")}, "min_delay_minutes": int(cfg.get("min_delay_minutes") or 3), "max_delay_minutes": int(cfg.get("max_delay_minutes") or 15), "resend_block_days": int(cfg.get("resend_block_days") or 90), "state": "draft", "created_at": stamp, "updated_at": stamp}
    campaign = store.insert("campaigns", {"id": new_id(), **data}); return RedirectResponse(f"/campaigns/{campaign['id']}", 303)



@app.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
def campaign_detail(request: Request, campaign_id: str):
    campaign = store.get("campaigns", campaign_id)
    if not campaign: raise HTTPException(404)
    logs = store.list("email_log", {"campaign_id": campaign_id}, order="scheduled_for asc", limit=1000)
    clients_map = {c["id"]: c for c in store.list("clients", limit=10000)}
    templates_map = {t["id"]: t for t in store.list("email_templates", limit=100)}
    for l in logs:
        l["client"] = clients_map.get(l.get("client_id")) or {}
        l["template"] = templates_map.get(l.get("template_id")) or {}
    cfg = store.get("settings", 1) or {}
    gmail_account_emails = {str(row["id"]): row.get("email") for row in list_gmail_senders(storage=store, cfg=cfg)}
    performance = campaign_performance(logs, {str(key): value for key, value in templates_map.items()})
    return page(request, "campaign.html", campaign=campaign, logs=logs, gmail_account_emails=gmail_account_emails, performance=performance)


@app.post("/campaigns/{campaign_id}/start")
def campaign_start(campaign_id: str):
    result = queue_campaign(campaign_id); return RedirectResponse(f"/campaigns/{campaign_id}?notice=Queued+{result['queued']}%2C+skipped+{result['skipped']}", 303)


@app.post("/campaigns/{campaign_id}/state")
def campaign_state(campaign_id: str, state: str = Form(...)):
    if state not in {"running", "paused", "stopped"}: raise HTTPException(422)
    if state == "stopped":
        cancel_campaign_queue(campaign_id)
    store.update("campaigns", campaign_id, {"state": state, "stopped_at": now_iso() if state == "stopped" else None, "updated_at": now_iso()}); return RedirectResponse(f"/campaigns/{campaign_id}", 303)


@app.post("/campaigns/{campaign_id}/delete")
def campaign_delete(campaign_id: str):
    try:
        delete_campaign(campaign_id)
    except ValueError as exc:
        return RedirectResponse(f"/campaigns/{campaign_id}?notice={quote(str(exc))}", 303)
    return RedirectResponse("/campaigns?notice=Campaign+deleted", 303)


@app.post("/email-log/{log_id}/send-now")
def email_send_now(request: Request, log_id: str):
    res = send_due(1, only_id=log_id)
    log = store.get("email_log", log_id) or {}
    cid = log.get("campaign_id")
    base = f"/campaigns/{cid}" if cid else request.headers.get("referer") or "/"
    if res.get("sent", 0) > 0:
        return RedirectResponse(f"{base}?notice=Email+delivered+live+via+Gmail!", 303)
    elif res.get("deferred", 0) > 0:
        return RedirectResponse(f"{base}?notice=Global+sender+cap+reached.+Email+kept+queued+for+the+next+send+window.", 303)
    else:
        err = log.get("error") or "Delivery failed"
        return RedirectResponse(f"{base}?notice=Delivery+failed:+{err}", 303)


@app.get("/api/locations/search")
def location_search(q: str = ""):
    return JSONResponse(search_locations(q, limit=6))


@app.get("/scraper", response_class=HTMLResponse)
def scraper_page(request: Request):
    return page(request, "scraper.html", jobs=store.list("scrape_jobs", limit=200), discards=store.list("scrape_discards", limit=100), presets=store.list("scrape_presets"), state_labels={s: label_for_state(s) for s in ("TX", "CA")})


@app.post("/scraper/jobs")
def scraper_create(categories: str = Form(...), locations: str = Form(...), result_limit: int = Form(30)):
    cats = [x.strip() for x in categories.split(",") if x.strip()]; locs = [x.strip() for x in locations.split(";") if x.strip()]
    parent = new_id(); created = 0
    for category in cats:
        for location in locs:
            bits = [x.strip() for x in location.rsplit(",", 1)]; city, state = (bits[0], bits[1].upper()) if len(bits) == 2 else (bits[0], "")
            stamp = now_iso(); jid = new_id(); store.insert("scrape_jobs", {"id": jid, "parent_id": parent, "category": category, "state": state, "city": city, "result_limit": min(max(result_limit, 1), 100), "status": "queued", "found_count": 0, "saved_count": 0, "discarded_count": 0, "serp_calls_used": 0, "created_at": stamp, "updated_at": stamp})
            store.insert("jobs", {"id": new_id(), "kind": "scrape", "payload": {"scrape_job_id": jid}, "status": "queued", "attempts": 0, "run_after": stamp, "created_at": stamp, "updated_at": stamp}); created += 1
    return RedirectResponse(f"/scraper?notice=Created+{created}+jobs", 303)


@app.post("/scraper/presets")
def preset_save(name: str = Form(...), categories: str = Form(...), locations: str = Form(...), result_limit: int = Form(30)):
    stamp = now_iso(); store.insert("scrape_presets", {"id": new_id(), "name": name, "config": {"categories": categories, "locations": locations, "result_limit": result_limit}, "created_at": stamp, "updated_at": stamp}); return RedirectResponse("/scraper", 303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    cfg = store.get("settings", 1) or {}
    return page(request, "settings.html", gmail_senders=list_gmail_senders(storage=store, cfg=cfg))


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form(); previous = store.get("settings", 1) or {}; fields = ["sender_name", "sender_email", "reply_to", "sender_business", "sender_business_url", "product_name", "product_url", "booking_link", "callback_number", "client_noun", "email_signature", "business_context", "timezone", "send_start", "send_end"]
    data = {key: str(form.get(key) or "").strip() for key in fields}
    for key in ("client_count", "daily_cap", "min_delay_minutes", "max_delay_minutes", "resend_block_days", "pingram_daily_cap", "pingram_min_delay", "pingram_max_delay"):
        if form.get(key) is not None and str(form.get(key)).strip() != "":
            data[key] = int(form.get(key))
    data["send_days"] = [int(x) for x in form.getlist("send_days")]; data["csv_required_fields"] = form.getlist("csv_required_fields"); data["scrape_required_fields"] = form.getlist("scrape_required_fields"); data["updated_at"] = now_iso()
    store.update("settings", 1, data)
    schedule_fields = {"daily_cap", "timezone", "send_days", "send_start", "send_end", "min_delay_minutes", "max_delay_minutes", "pingram_daily_cap", "pingram_min_delay", "pingram_max_delay"}
    schedule_changed = any(previous.get(key) != data.get(key) for key in schedule_fields)
    if schedule_changed:
        pending = store.list("jobs", {"kind": "reschedule_email_queue", "status": ("in", ["queued", "running"])}, order="", limit=1)
        if not pending:
            stamp = now_iso(); store.insert("jobs", {"id": new_id(), "kind": "reschedule_email_queue", "payload": {}, "status": "queued", "attempts": 0, "run_after": stamp, "created_at": stamp, "updated_at": stamp})
    notice = "Saved.+Queued+emails+will+be+redistributed+to+the+global+sender+cap." if schedule_changed else "Saved"
    return RedirectResponse(f"/settings?notice={notice}", 303)


@app.post("/settings/gmail-senders")
async def gmail_sender_add(request: Request):
    form = await request.form()
    provider = str(form.get("provider") or "pingram").strip().lower()
    email = normalize_email(str(form.get("email") or ""))
    password = str(form.get("app_password") or "").replace(" ", "").strip()
    
    if not valid_email(email):
        return RedirectResponse("/settings?notice=Valid+email+is+required", 303)
    if provider == "gmail" and not password:
        return RedirectResponse("/settings?notice=App+password+is+required+for+Gmail+senders", 303)
    if any(str(row.get("email") or "").lower() == email for row in list_gmail_senders(storage=store)):
        return RedirectResponse("/settings?notice=That+sender+already+exists", 303)
        
    stamp = now_iso()
    reply_to = normalize_email(str(form.get("reply_to") or "")) or ("outreach@contractorops.ai" if provider == "pingram" else email)
    store.insert("gmail_senders", {
        "id": new_id(),
        "email": email,
        "display_name": str(form.get("display_name") or "").strip() or "Outreach",
        "signature": str(form.get("signature") or "").strip(),
        "reply_to": reply_to,
        "app_password_encrypted": encrypt_secret(password) if password else "",
        "active": True,
        "provider": provider,
        "created_at": stamp,
        "updated_at": stamp
    })
    return RedirectResponse(f"/settings?notice={'Pingram' if provider == 'pingram' else 'Gmail'}+sender+added", 303)


@app.post("/settings/gmail-senders/{sender_id}")
async def gmail_sender_update(request: Request, sender_id: str):
    sender = store.get("gmail_senders", sender_id)
    if not sender: raise HTTPException(404)
    form = await request.form(); email = normalize_email(str(form.get("email") or ""))
    if not valid_email(email): return RedirectResponse("/settings?notice=Enter+a+valid+Gmail+address", 303)
    duplicate = [row for row in list_gmail_senders(storage=store) if str(row.get("id")) != sender_id and str(row.get("email") or "").lower() == email]
    if duplicate: return RedirectResponse("/settings?notice=That+Gmail+sender+already+exists", 303)
    values = {"email": email, "display_name": str(form.get("display_name") or "").strip() or "Outreach", "signature": str(form.get("signature") or "").strip(), "reply_to": normalize_email(str(form.get("reply_to") or "")) or email, "active": form.get("active") == "on", "updated_at": now_iso()}
    password = str(form.get("app_password") or "").replace(" ", "").strip()
    if password: values["app_password_encrypted"] = encrypt_secret(password)
    store.update("gmail_senders", sender_id, values)
    return RedirectResponse("/settings?notice=Gmail+sender+updated", 303)


@app.post("/settings/test-email")
def test_email(to: str = Form(...), gmail_sender_id: str = Form(...)):
    cfg = store.get("settings", 1) or {}; sender = get_gmail_sender(gmail_sender_id, storage=store, cfg=cfg)
    if not sender: return RedirectResponse("/settings?notice=Sender+not+found", 303)
    identity = sender_context(cfg, sender); provider = get_provider(sender.get("provider") or "gmail", cfg, gmail_sender_id, gmail_sender=sender)
    result = provider.send(to=to, subject="Outreach email test", body=f"Your sender {identity['sender_name']} ({identity['sender_email']}) is connected.\n\n{identity['signature']}", content_type="plain", from_address=identity["sender_email"], from_name=identity["sender_name"], reply_to=identity["reply_to"])
    msg = "Test+sent" if result.ok else f"Test+failed:+{quote(str(result.error or 'unknown'))}"
    return RedirectResponse(f"/settings?notice={msg}", 303)


@app.api_route("/webhooks/pingram", methods=["GET", "POST"])
@app.api_route("/webhooks/pingram/", methods=["GET", "POST"])
async def pingram_webhook(request: Request):
    if request.method == "GET":
        return JSONResponse({"status": "ok", "service": "pingram-webhook-receiver"})
    
    import email.utils
    try:
        payload = await request.json()
    except Exception:
        payload = {}
        
    logging.info(f"Pingram webhook received payload: {payload}")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    
    tracking = payload.get("trackingId") or payload.get("id") or data.get("trackingId") or data.get("id")
    event = str(payload.get("eventType") or payload.get("type") or data.get("eventType") or data.get("type") or "").lower()
    
    if event in {"inbound", "email_inbound"}:
        raw_from = str(data.get("fromAddress") or data.get("from") or data.get("sender") or payload.get("fromAddress") or "").strip()
        _, client_email = email.utils.parseaddr(raw_from)
        client_email = (client_email or raw_from).lower().strip()
        
        subject = data.get("subject") or payload.get("subject") or "(No Subject)"
        body_text = (
            data.get("bodyText") or
            data.get("text") or
            data.get("body") or
            data.get("bodyHtml") or
            data.get("html") or
            payload.get("bodyText") or
            payload.get("text") or
            payload.get("body") or
            payload.get("bodyHtml") or
            payload.get("html") or
            ""
        )
        
        if client_email:
            clients = [c for c in store.list("clients", order="", limit=10000) if str(c.get("email") or "").lower() == client_email]
            client_id = clients[0]["id"] if clients else None
            
            store.insert("pingram_replies", {
                "id": new_id(),
                "client_id": client_id,
                "from_email": client_email,
                "subject": subject,
                "body_text": body_text,
                "received_at": now_iso()
            })
            if client_id:
                set_client_status(client_id, "replied", storage=store)
            logging.info(f"Pingram reply recorded for email {client_email}, client_id: {client_id}")
            
        return JSONResponse({"ok": True})

    logs = [x for x in store.list("email_log", order="", limit=20000) if x.get("provider_message_id") == tracking]
    if logs and event in {"email_bounced", "bounced", "email_failed"}:
        store.update("email_log", logs[0]["id"], {"status": "bounced"})
        set_client_status(logs[0]["client_id"], "bounced", storage=store)
        refresh_campaign_states({str(logs[0].get("campaign_id"))})
    return JSONResponse({"ok": True})


@app.get("/pingram-inbox")
def pingram_inbox(request: Request):
    replies_raw = store.list("pingram_replies", order="received_at desc", limit=1000)
    clients = {c["id"]: c for c in store.list("clients", order="", limit=10000)}
    replies = []
    for r in replies_raw:
        client = clients.get(r.get("client_id"))
        replies.append({
            "id": r.get("id"),
            "client_id": r.get("client_id"),
            "from_email": r.get("from_email"),
            "company": client.get("business_name") if client else "",
            "client_name": (client.get("owner_first_name") or client.get("short_name")) if client else "",
            "received_at": r.get("received_at")[:16].replace("T", " ") if r.get("received_at") else "",
            "subject": r.get("subject"),
            "body_text": r.get("body_text")
        })
    return page(request, "inbox.html", replies=replies)


@app.post("/pingram-inbox/{reply_id}/delete")
def pingram_reply_delete(reply_id: str):
    store.delete("pingram_replies", {"id": reply_id})
    return RedirectResponse("/pingram-inbox?notice=Reply+dismissed", 303)


@app.get("/health")
def health(): return {"ok": True, "database": "supabase" if env.using_supabase else "sqlite", "dry_run": env.DRY_RUN}
