"""Tasko Real Estate CRM - FastAPI backend.

Single-file implementation for the MVP. Handles auth, projects, inventory,
leads (with source webhooks), site visits, follow-ups, and analytics.
"""

from dotenv import load_dotenv
from pathlib import Path
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import os
import logging
import secrets
import uuid
import random
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Literal

import bcrypt
import jwt
from fastapi import FastAPI, APIRouter, Depends, HTTPException, Request, Response, status, Query
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field, EmailStr, ConfigDict


# ---------------------------------------------------------------------------
# Config & DB
# ---------------------------------------------------------------------------
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-me")
JWT_ALGO = "HS256"
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@tasko.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

app = FastAPI(title="Tasko CRM API")
api = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
log = logging.getLogger("tasko")


# ---------------------------------------------------------------------------
# Utils
# ---------------------------------------------------------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False


def create_token(user_id: str, email: str, role: str, kind: str = "access", ttl_min: int = 60 * 12) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "type": kind,
        "exp": now_utc() + timedelta(minutes=ttl_min),
        "iat": now_utc(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])


async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = await db.users.find_one({"id": payload["sub"]}, {"password_hash": 0})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    user.pop("_id", None)
    return user


def require_roles(*roles: str):
    async def dep(user: dict = Depends(get_current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user
    return dep


def set_auth_cookies(resp: Response, user_id: str, email: str, role: str) -> None:
    access = create_token(user_id, email, role, "access", ttl_min=60 * 12)
    refresh = create_token(user_id, email, role, "refresh", ttl_min=60 * 24 * 7)
    resp.set_cookie("access_token", access, httponly=True, secure=True, samesite="none", max_age=60 * 60 * 12, path="/")
    resp.set_cookie("refresh_token", refresh, httponly=True, secure=True, samesite="none", max_age=60 * 60 * 24 * 7, path="/")


def clean(doc: dict) -> dict:
    if not doc:
        return doc
    doc.pop("_id", None)
    doc.pop("password_hash", None)
    return doc


# ---------------------------------------------------------------------------
# Models (pydantic schemas for requests / responses)
# ---------------------------------------------------------------------------
Role = Literal["admin", "manager", "executive"]
LeadStage = Literal["new", "contacted", "qualified", "site_visit", "negotiation", "booked", "lost"]
LeadSource = Literal["magicbricks", "99acres", "commonfloor", "housing", "website", "google_ads", "facebook", "instagram", "referral", "walk_in", "manual"]
UnitStatus = Literal["available", "held", "booked", "sold"]
VisitStatus = Literal["scheduled", "completed", "no_show", "cancelled"]
FollowUpStatus = Literal["pending", "done", "missed"]


class BaseDoc(BaseModel):
    model_config = ConfigDict(extra="ignore")


class LoginBody(BaseDoc):
    email: EmailStr
    password: str


class RegisterBody(BaseDoc):
    email: EmailStr
    password: str
    name: str
    role: Role = "executive"
    phone: Optional[str] = None


class UpdateUserBody(BaseDoc):
    name: Optional[str] = None
    role: Optional[Role] = None
    phone: Optional[str] = None
    active: Optional[bool] = None
    password: Optional[str] = None


class ProjectBody(BaseDoc):
    name: str
    location: str
    city: Optional[str] = None
    rera: Optional[str] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    configurations: Optional[List[str]] = None
    cover: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = "active"


class UnitBody(BaseDoc):
    project_id: str
    tower: str
    floor: int
    unit_no: str
    config: str  # e.g. 2BHK
    carpet_area: Optional[float] = None
    price: Optional[float] = None
    facing: Optional[str] = None
    status: UnitStatus = "available"


class LeadBody(BaseDoc):
    name: str
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    source: LeadSource = "manual"
    project_id: Optional[str] = None
    budget_min: Optional[float] = None
    budget_max: Optional[float] = None
    configuration: Optional[str] = None
    location_pref: Optional[str] = None
    notes: Optional[str] = None
    assigned_to: Optional[str] = None
    stage: LeadStage = "new"
    stars: Optional[int] = 0


class UpdateLeadBody(BaseDoc):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    source: Optional[LeadSource] = None
    project_id: Optional[str] = None
    budget_min: Optional[float] = None
    budget_max: Optional[float] = None
    configuration: Optional[str] = None
    location_pref: Optional[str] = None
    notes: Optional[str] = None
    assigned_to: Optional[str] = None
    stage: Optional[LeadStage] = None
    lost_reason: Optional[str] = None
    priority: Optional[Literal["hot", "warm", "cold"]] = None
    stars: Optional[int] = None


class AssignBody(BaseDoc):
    user_id: str


class StageBody(BaseDoc):
    stage: LeadStage
    note: Optional[str] = None


class NoteBody(BaseDoc):
    text: str
    kind: Literal["note", "call", "whatsapp", "email"] = "note"


class SiteVisitBody(BaseDoc):
    lead_id: str
    project_id: str
    scheduled_at: datetime
    assigned_to: Optional[str] = None
    notes: Optional[str] = None


class UpdateSiteVisitBody(BaseDoc):
    scheduled_at: Optional[datetime] = None
    status: Optional[VisitStatus] = None
    notes: Optional[str] = None
    outcome: Optional[str] = None
    assigned_to: Optional[str] = None


class FollowUpBody(BaseDoc):
    lead_id: str
    due_at: datetime
    kind: Literal["call", "whatsapp", "email", "meeting"] = "call"
    notes: Optional[str] = None
    assigned_to: Optional[str] = None


class UpdateFollowUpBody(BaseDoc):
    due_at: Optional[datetime] = None
    status: Optional[FollowUpStatus] = None
    notes: Optional[str] = None


class SettingsBody(BaseDoc):
    whatsapp_enabled: Optional[bool] = None
    email_enabled: Optional[bool] = None
    google_calendar_enabled: Optional[bool] = None
    whatsapp_number: Optional[str] = None
    resend_from_email: Optional[str] = None
    auto_assign_enabled: Optional[bool] = None
    auto_followup_enabled: Optional[bool] = None
    auto_call_on_new_lead: Optional[bool] = None
    missed_call_followup_enabled: Optional[bool] = None
    missed_call_followup_hours: Optional[float] = None


# ---------------------------------------------------------------------------
# Activity helper
# ---------------------------------------------------------------------------
async def log_activity(lead_id: Optional[str], actor: dict, kind: str, message: str, meta: Optional[dict] = None) -> None:
    doc = {
        "id": new_id(),
        "lead_id": lead_id,
        "actor_id": actor.get("id") if actor else None,
        "actor_name": actor.get("name") if actor else "system",
        "kind": kind,
        "message": message,
        "meta": meta or {},
        "created_at": now_utc().isoformat(),
    }
    await db.activities.insert_one(doc)


# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------
@api.post("/auth/login")
async def login(body: LoginBody, response: Response):
    email = body.email.lower().strip()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(body.password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if user.get("active") is False:
        raise HTTPException(status_code=403, detail="Account is disabled")
    set_auth_cookies(response, user["id"], user["email"], user["role"])
    return clean(user)


@api.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")
    return {"ok": True}


@api.get("/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return user


@api.post("/auth/refresh")
async def refresh(request: Request, response: Response):
    tok = request.cookies.get("refresh_token")
    if not tok:
        raise HTTPException(status_code=401, detail="No refresh token")
    try:
        payload = decode_token(tok)
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid token type")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    user = await db.users.find_one({"id": payload["sub"]}, {"password_hash": 0})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    set_auth_cookies(response, user["id"], user["email"], user["role"])
    return clean(user)


# ---------------------------------------------------------------------------
# USERS / TEAM
# ---------------------------------------------------------------------------
@api.get("/users")
async def list_users(user: dict = Depends(get_current_user)):
    docs = await db.users.find({}, {"password_hash": 0, "_id": 0}).sort("created_at", -1).to_list(500)
    return docs


@api.post("/users")
async def create_user(body: RegisterBody, actor: dict = Depends(require_roles("admin"))):
    email = body.email.lower().strip()
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Email already exists")
    doc = {
        "id": new_id(),
        "email": email,
        "name": body.name,
        "role": body.role,
        "password_hash": hash_password(body.password),
        "active": True,
        "created_at": now_utc().isoformat(),
    }
    await db.users.insert_one(doc)
    return clean(doc)


class UpdateSelfBody(BaseDoc):
    name: Optional[str] = None
    phone: Optional[str] = None
    password: Optional[str] = None


@api.patch("/users/me")
async def update_self(body: UpdateSelfBody, actor: dict = Depends(get_current_user)):
    update = {k: v for k, v in body.model_dump(exclude_none=True).items() if k != "password"}
    if body.password:
        update["password_hash"] = hash_password(body.password)
    if not update:
        raise HTTPException(status_code=400, detail="Nothing to update")
    await db.users.update_one({"id": actor["id"]}, {"$set": update})
    return await db.users.find_one({"id": actor["id"]}, {"password_hash": 0, "_id": 0})


@api.patch("/users/{user_id}")
async def update_user(user_id: str, body: UpdateUserBody, actor: dict = Depends(require_roles("admin"))):
    update = {k: v for k, v in body.model_dump(exclude_none=True).items() if k != "password"}
    if body.password:
        update["password_hash"] = hash_password(body.password)
    if not update:
        raise HTTPException(status_code=400, detail="Nothing to update")
    result = await db.users.update_one({"id": user_id}, {"$set": update})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    doc = await db.users.find_one({"id": user_id}, {"password_hash": 0, "_id": 0})
    return doc


@api.delete("/users/{user_id}")
async def delete_user(user_id: str, actor: dict = Depends(require_roles("admin"))):
    if user_id == actor["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    r = await db.users.delete_one({"id": user_id})
    if r.deleted_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# PROJECTS
# ---------------------------------------------------------------------------
@api.get("/projects")
async def list_projects(user: dict = Depends(get_current_user)):
    docs = await db.projects.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)
    # attach counts
    for d in docs:
        d["units_total"] = await db.units.count_documents({"project_id": d["id"]})
        d["units_available"] = await db.units.count_documents({"project_id": d["id"], "status": "available"})
        d["leads_count"] = await db.leads.count_documents({"project_id": d["id"]})
    return docs


@api.post("/projects")
async def create_project(body: ProjectBody, actor: dict = Depends(require_roles("admin", "manager"))):
    doc = body.model_dump()
    doc["id"] = new_id()
    doc["created_at"] = now_utc().isoformat()
    await db.projects.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api.patch("/projects/{project_id}")
async def update_project(project_id: str, body: ProjectBody, actor: dict = Depends(require_roles("admin", "manager"))):
    update = body.model_dump(exclude_none=True)
    r = await db.projects.update_one({"id": project_id}, {"$set": update})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Project not found")
    return await db.projects.find_one({"id": project_id}, {"_id": 0})


@api.delete("/projects/{project_id}")
async def delete_project(project_id: str, actor: dict = Depends(require_roles("admin"))):
    await db.projects.delete_one({"id": project_id})
    await db.units.delete_many({"project_id": project_id})
    return {"ok": True}


# ---------------------------------------------------------------------------
# INVENTORY / UNITS
# ---------------------------------------------------------------------------
@api.get("/units")
async def list_units(project_id: Optional[str] = None, user: dict = Depends(get_current_user)):
    q = {}
    if project_id:
        q["project_id"] = project_id
    docs = await db.units.find(q, {"_id": 0}).sort([("tower", 1), ("floor", 1), ("unit_no", 1)]).to_list(2000)
    return docs


@api.post("/units")
async def create_unit(body: UnitBody, actor: dict = Depends(require_roles("admin", "manager"))):
    doc = body.model_dump()
    doc["id"] = new_id()
    doc["created_at"] = now_utc().isoformat()
    await db.units.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api.patch("/units/{unit_id}")
async def update_unit(unit_id: str, body: dict, actor: dict = Depends(require_roles("admin", "manager"))):
    allowed = {"status", "price", "carpet_area", "facing", "config", "tower", "floor", "unit_no"}
    update = {k: v for k, v in body.items() if k in allowed}
    if not update:
        raise HTTPException(status_code=400, detail="Nothing to update")
    r = await db.units.update_one({"id": unit_id}, {"$set": update})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Unit not found")
    return await db.units.find_one({"id": unit_id}, {"_id": 0})


@api.delete("/units/{unit_id}")
async def delete_unit(unit_id: str, actor: dict = Depends(require_roles("admin", "manager"))):
    await db.units.delete_one({"id": unit_id})
    return {"ok": True}


# ---------------------------------------------------------------------------
# LEADS
# ---------------------------------------------------------------------------
async def _pick_auto_assignee() -> Optional[str]:
    """Round-robin among executives that are active."""
    execs = await db.users.find({"role": "executive", "active": {"$ne": False}}, {"id": 1, "_id": 0}).to_list(200)
    if not execs:
        return None
    # pick executive with the fewest open (non-terminal) leads
    counts = []
    for e in execs:
        c = await db.leads.count_documents({"assigned_to": e["id"], "stage": {"$nin": ["booked", "lost"]}})
        counts.append((c, e["id"]))
    counts.sort()
    return counts[0][1]


@api.get("/leads")
async def list_leads(
    project_id: Optional[str] = None,
    stage: Optional[str] = None,
    assigned_to: Optional[str] = None,
    source: Optional[str] = None,
    search: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    q: dict = {}
    if project_id:
        q["project_id"] = project_id
    if stage:
        q["stage"] = stage
    if assigned_to:
        q["assigned_to"] = assigned_to
    if source:
        q["source"] = source
    if search:
        q["$or"] = [
            {"name": {"$regex": search, "$options": "i"}},
            {"phone": {"$regex": search, "$options": "i"}},
            {"email": {"$regex": search, "$options": "i"}},
        ]
    # executives only see their leads
    if user["role"] == "executive":
        q["assigned_to"] = user["id"]
    docs = await db.leads.find(q, {"_id": 0}).sort("created_at", -1).to_list(2000)
    return docs


@api.get("/leads/{lead_id}")
async def get_lead(lead_id: str, user: dict = Depends(get_current_user)):
    doc = await db.leads.find_one({"id": lead_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Lead not found")
    if user["role"] == "executive" and doc.get("assigned_to") != user["id"]:
        raise HTTPException(status_code=403, detail="Not your lead")
    return doc


@api.post("/leads")
async def create_lead(body: LeadBody, actor: dict = Depends(get_current_user)):
    doc = body.model_dump()
    doc["id"] = new_id()
    doc["created_at"] = now_utc().isoformat()
    doc["updated_at"] = doc["created_at"]
    doc.setdefault("priority", "warm")
    if not doc.get("assigned_to"):
        settings = await db.settings.find_one({"id": "singleton"}) or {}
        if settings.get("auto_assign_enabled", True):
            doc["assigned_to"] = await _pick_auto_assignee()
    await db.leads.insert_one(doc)
    doc.pop("_id", None)
    await log_activity(doc["id"], actor, "lead_created", f"Lead created from {doc['source']}")
    # Notify the assigned executive
    if doc.get("assigned_to"):
        await create_notification(
            type="lead_assigned",
            title="New lead assigned",
            message=f"{doc['name']} · {doc.get('source', 'manual')}",
            user_id=doc["assigned_to"],
            link=f"/leads/{doc['id']}",
            meta={"lead_id": doc["id"]},
        )
    # Auto-call on new lead assignment (Twilio)
    settings = await db.settings.find_one({"id": "singleton"}) or {}
    if settings.get("auto_call_on_new_lead") and doc.get("assigned_to") and doc.get("phone"):
        assignee = await db.users.find_one({"id": doc["assigned_to"]})
        if assignee and assignee.get("phone"):
            try:
                await _initiate_twilio_call(doc, assignee)
            except HTTPException as e:
                log.warning("auto-call skipped: %s", e.detail)
            except Exception as e:
                log.warning("auto-call error: %s", e)
    return doc


@api.patch("/leads/{lead_id}")
async def update_lead(lead_id: str, body: UpdateLeadBody, actor: dict = Depends(get_current_user)):
    lead = await db.leads.find_one({"id": lead_id})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    # Executives can only edit their own leads. Admin/manager can edit any.
    if actor["role"] == "executive" and lead.get("assigned_to") != actor["id"]:
        raise HTTPException(status_code=403, detail="You can only edit your own leads")
    update = body.model_dump(exclude_none=True)
    if not update:
        raise HTTPException(status_code=400, detail="Nothing to update")
    update["updated_at"] = now_utc().isoformat()
    r = await db.leads.update_one({"id": lead_id}, {"$set": update})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Lead not found")
    if "stage" in update:
        await log_activity(lead_id, actor, "stage_change", f"Stage moved to {update['stage']}")
    if "assigned_to" in update:
        await log_activity(lead_id, actor, "assignment", f"Assigned to user {update['assigned_to']}")
    return await db.leads.find_one({"id": lead_id}, {"_id": 0})


@api.post("/leads/{lead_id}/assign")
async def assign_lead(lead_id: str, body: AssignBody, actor: dict = Depends(require_roles("admin", "manager"))):
    r = await db.leads.update_one(
        {"id": lead_id},
        {"$set": {"assigned_to": body.user_id, "updated_at": now_utc().isoformat()}},
    )
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Lead not found")
    assignee = await db.users.find_one({"id": body.user_id}, {"name": 1, "_id": 0})
    await log_activity(lead_id, actor, "assignment", f"Assigned to {assignee.get('name', body.user_id) if assignee else body.user_id}")
    lead = await db.leads.find_one({"id": lead_id}, {"_id": 0})
    if lead:
        await create_notification(
            type="lead_assigned",
            title="New lead assigned",
            message=f"{lead.get('name', 'Lead')} · {lead.get('source', 'manual')}",
            user_id=body.user_id,
            link=f"/leads/{lead_id}",
            meta={"lead_id": lead_id},
        )
    return lead


@api.post("/leads/{lead_id}/stage")
async def move_stage(lead_id: str, body: StageBody, actor: dict = Depends(get_current_user)):
    update = {"stage": body.stage, "updated_at": now_utc().isoformat()}
    r = await db.leads.update_one({"id": lead_id}, {"$set": update})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Lead not found")
    await log_activity(lead_id, actor, "stage_change", f"Stage → {body.stage}" + (f": {body.note}" if body.note else ""))
    return await db.leads.find_one({"id": lead_id}, {"_id": 0})


@api.post("/leads/{lead_id}/notes")
async def add_note(lead_id: str, body: NoteBody, actor: dict = Depends(get_current_user)):
    await log_activity(lead_id, actor, body.kind, body.text)
    return {"ok": True}


@api.delete("/leads/{lead_id}")
async def delete_lead(lead_id: str, actor: dict = Depends(require_roles("admin"))):
    r = await db.leads.delete_one({"id": lead_id})
    if r.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Lead not found")
    await db.activities.delete_many({"lead_id": lead_id})
    return {"ok": True}


# ---------------------------------------------------------------------------
# LEAD WEBHOOKS — generic receiver for external platforms
# ---------------------------------------------------------------------------
async def _ingest_lead_from_payload(source: str, payload: dict) -> dict:
    """Normalize an incoming external lead payload into a lead document."""
    name = payload.get("name") or payload.get("full_name") or payload.get("customer_name") or "Unknown"
    phone = payload.get("phone") or payload.get("mobile") or payload.get("contact")
    email = payload.get("email") or payload.get("customer_email")
    project_name = payload.get("project") or payload.get("project_name")
    project = None
    if project_name:
        project = await db.projects.find_one({"name": {"$regex": f"^{project_name}$", "$options": "i"}}, {"id": 1})
    lead = {
        "id": new_id(),
        "name": name,
        "phone": phone,
        "email": email,
        "source": source,
        "project_id": project["id"] if project else None,
        "budget_min": payload.get("budget_min"),
        "budget_max": payload.get("budget_max"),
        "configuration": payload.get("configuration") or payload.get("bhk"),
        "location_pref": payload.get("location") or payload.get("locality"),
        "notes": payload.get("message") or payload.get("notes"),
        "stage": "new",
        "priority": "warm",
        "created_at": now_utc().isoformat(),
        "updated_at": now_utc().isoformat(),
    }
    # auto-assign
    settings = await db.settings.find_one({"id": "singleton"}) or {}
    if settings.get("auto_assign_enabled", True):
        lead["assigned_to"] = await _pick_auto_assignee()
    await db.leads.insert_one(lead)
    lead.pop("_id", None)
    await log_activity(lead["id"], None, "lead_created", f"Auto-captured from {source}", {"raw": payload})
    if lead.get("assigned_to"):
        await create_notification(
            type="lead_assigned",
            title="New lead assigned",
            message=f"{lead['name']} · {source}",
            user_id=lead["assigned_to"],
            link=f"/leads/{lead['id']}",
            meta={"lead_id": lead["id"]},
        )
    settings = await db.settings.find_one({"id": "singleton"}) or {}
    if settings.get("auto_call_on_new_lead") and lead.get("assigned_to") and lead.get("phone"):
        assignee = await db.users.find_one({"id": lead["assigned_to"]})
        if assignee and assignee.get("phone"):
            try:
                await _initiate_twilio_call(lead, assignee)
            except Exception as e:
                log.warning("auto-call (webhook) skipped: %s", e)
    return lead


@api.post("/webhooks/leads/{source}")
async def webhook_leads(source: str, payload: dict):
    """Public endpoint. External platforms POST here to push leads.

    Accepts sources: magicbricks, 99acres, commonfloor, housing, website,
    google_ads, facebook, instagram, referral, walk_in.
    """
    allowed = {"magicbricks", "99acres", "commonfloor", "housing", "website", "google_ads", "facebook", "instagram", "referral", "walk_in"}
    if source not in allowed:
        raise HTTPException(status_code=400, detail=f"Unknown source '{source}'")
    lead = await _ingest_lead_from_payload(source, payload)
    return {"ok": True, "lead_id": lead["id"]}


# ---------------------------------------------------------------------------
# SITE VISITS
# ---------------------------------------------------------------------------
@api.get("/site-visits")
async def list_visits(project_id: Optional[str] = None, lead_id: Optional[str] = None, user: dict = Depends(get_current_user)):
    q: dict = {}
    if project_id:
        q["project_id"] = project_id
    if lead_id:
        q["lead_id"] = lead_id
    if user["role"] == "executive":
        q["assigned_to"] = user["id"]
    docs = await db.site_visits.find(q, {"_id": 0}).sort("scheduled_at", 1).to_list(2000)
    return docs


@api.post("/site-visits")
async def create_visit(body: SiteVisitBody, actor: dict = Depends(get_current_user)):
    doc = body.model_dump()
    doc["id"] = new_id()
    doc["status"] = "scheduled"
    doc["scheduled_at"] = doc["scheduled_at"].astimezone(timezone.utc).isoformat()
    doc["created_at"] = now_utc().isoformat()
    if not doc.get("assigned_to"):
        lead = await db.leads.find_one({"id": doc["lead_id"]}, {"assigned_to": 1})
        doc["assigned_to"] = (lead or {}).get("assigned_to")
    await db.site_visits.insert_one(doc)
    doc.pop("_id", None)
    # advance lead to site_visit stage
    await db.leads.update_one({"id": doc["lead_id"]}, {"$set": {"stage": "site_visit", "updated_at": now_utc().isoformat()}})
    await log_activity(doc["lead_id"], actor, "site_visit_scheduled", f"Site visit scheduled at {doc['scheduled_at']}")
    if doc.get("assigned_to"):
        lead = await db.leads.find_one({"id": doc["lead_id"]}, {"name": 1, "_id": 0})
        await create_notification(
            type="sitevisit_reminder",
            title="Site visit scheduled",
            message=f"{(lead or {}).get('name', 'Lead')} · {doc['scheduled_at'][:16].replace('T', ' ')}",
            user_id=doc["assigned_to"],
            link=f"/leads/{doc['lead_id']}",
            meta={"visit_id": doc["id"]},
        )
    return doc


@api.patch("/site-visits/{visit_id}")
async def update_visit(visit_id: str, body: UpdateSiteVisitBody, actor: dict = Depends(get_current_user)):
    update = body.model_dump(exclude_none=True)
    if "scheduled_at" in update and isinstance(update["scheduled_at"], datetime):
        update["scheduled_at"] = update["scheduled_at"].astimezone(timezone.utc).isoformat()
    if not update:
        raise HTTPException(status_code=400, detail="Nothing to update")
    r = await db.site_visits.update_one({"id": visit_id}, {"$set": update})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Site visit not found")
    v = await db.site_visits.find_one({"id": visit_id}, {"_id": 0})
    if "status" in update:
        await log_activity(v["lead_id"], actor, "site_visit_" + update["status"], f"Site visit {update['status']}")
    return v


@api.delete("/site-visits/{visit_id}")
async def delete_visit(visit_id: str, actor: dict = Depends(require_roles("admin", "manager"))):
    await db.site_visits.delete_one({"id": visit_id})
    return {"ok": True}


# ---------------------------------------------------------------------------
# FOLLOW-UPS
# ---------------------------------------------------------------------------
@api.get("/follow-ups")
async def list_followups(lead_id: Optional[str] = None, status_q: Optional[str] = Query(None, alias="status"), user: dict = Depends(get_current_user)):
    q: dict = {}
    if lead_id:
        q["lead_id"] = lead_id
    if status_q:
        q["status"] = status_q
    if user["role"] == "executive":
        q["assigned_to"] = user["id"]
    docs = await db.follow_ups.find(q, {"_id": 0}).sort("due_at", 1).to_list(2000)
    return docs


@api.post("/follow-ups")
async def create_followup(body: FollowUpBody, actor: dict = Depends(get_current_user)):
    doc = body.model_dump()
    doc["id"] = new_id()
    doc["status"] = "pending"
    doc["due_at"] = doc["due_at"].astimezone(timezone.utc).isoformat()
    doc["created_at"] = now_utc().isoformat()
    if not doc.get("assigned_to"):
        lead = await db.leads.find_one({"id": doc["lead_id"]}, {"assigned_to": 1})
        doc["assigned_to"] = (lead or {}).get("assigned_to")
    await db.follow_ups.insert_one(doc)
    doc.pop("_id", None)
    await log_activity(doc["lead_id"], actor, "followup_scheduled", f"Follow-up scheduled at {doc['due_at']}")
    if doc.get("assigned_to"):
        lead = await db.leads.find_one({"id": doc["lead_id"]}, {"name": 1, "_id": 0})
        await create_notification(
            type="followup_due",
            title="Follow-up scheduled",
            message=f"{doc.get('kind', 'call').title()} · {(lead or {}).get('name', 'Lead')} · {doc['due_at'][:16].replace('T', ' ')}",
            user_id=doc["assigned_to"],
            link=f"/leads/{doc['lead_id']}",
            meta={"followup_id": doc["id"]},
        )
    return doc


@api.patch("/follow-ups/{fu_id}")
async def update_followup(fu_id: str, body: UpdateFollowUpBody, actor: dict = Depends(get_current_user)):
    update = body.model_dump(exclude_none=True)
    if "due_at" in update and isinstance(update["due_at"], datetime):
        update["due_at"] = update["due_at"].astimezone(timezone.utc).isoformat()
    if not update:
        raise HTTPException(status_code=400, detail="Nothing to update")
    r = await db.follow_ups.update_one({"id": fu_id}, {"$set": update})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Follow-up not found")
    return await db.follow_ups.find_one({"id": fu_id}, {"_id": 0})


@api.delete("/follow-ups/{fu_id}")
async def delete_followup(fu_id: str, actor: dict = Depends(get_current_user)):
    await db.follow_ups.delete_one({"id": fu_id})
    return {"ok": True}


# ---------------------------------------------------------------------------
# ACTIVITIES / TIMELINE
# ---------------------------------------------------------------------------
@api.get("/activities")
async def list_activities(lead_id: Optional[str] = None, limit: int = 50, user: dict = Depends(get_current_user)):
    q = {"lead_id": lead_id} if lead_id else {}
    docs = await db.activities.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit)
    return docs


# ---------------------------------------------------------------------------
# ANALYTICS
# ---------------------------------------------------------------------------
@api.get("/analytics/summary")
async def analytics_summary(project_id: Optional[str] = None, user: dict = Depends(get_current_user)):
    match: dict = {}
    if project_id:
        match["project_id"] = project_id
    if user["role"] == "executive":
        match["assigned_to"] = user["id"]

    total_leads = await db.leads.count_documents(match)

    # stage funnel
    stages = ["new", "contacted", "qualified", "site_visit", "negotiation", "booked", "lost"]
    funnel = {}
    for s in stages:
        funnel[s] = await db.leads.count_documents({**match, "stage": s})

    # source breakdown
    pipeline = [{"$match": match}, {"$group": {"_id": "$source", "count": {"$sum": 1}}}]
    sources = [{"source": r["_id"], "count": r["count"]} async for r in db.leads.aggregate(pipeline)]

    # visits & follow-ups today
    today_start = datetime.combine(now_utc().date(), datetime.min.time()).replace(tzinfo=timezone.utc).isoformat()
    today_end = datetime.combine(now_utc().date(), datetime.max.time()).replace(tzinfo=timezone.utc).isoformat()
    visits_today = await db.site_visits.count_documents({"scheduled_at": {"$gte": today_start, "$lte": today_end}})
    followups_pending = await db.follow_ups.count_documents({"status": "pending"})

    # last 7d trend
    trend = []
    for i in range(6, -1, -1):
        d = (now_utc() - timedelta(days=i)).date()
        start = datetime.combine(d, datetime.min.time()).replace(tzinfo=timezone.utc).isoformat()
        end = datetime.combine(d, datetime.max.time()).replace(tzinfo=timezone.utc).isoformat()
        cnt = await db.leads.count_documents({**match, "created_at": {"$gte": start, "$lte": end}})
        trend.append({"date": d.isoformat(), "leads": cnt})

    # conversion
    booked = funnel.get("booked", 0)
    conversion = round((booked / total_leads * 100), 1) if total_leads else 0.0

    return {
        "total_leads": total_leads,
        "conversion_rate": conversion,
        "visits_today": visits_today,
        "followups_pending": followups_pending,
        "funnel": funnel,
        "sources": sources,
        "trend": trend,
    }


# ---------------------------------------------------------------------------
# SETTINGS (integration toggles)
# ---------------------------------------------------------------------------
@api.get("/settings")
async def get_settings(user: dict = Depends(get_current_user)):
    s = await db.settings.find_one({"id": "singleton"}, {"_id": 0})
    if not s:
        s = {
            "id": "singleton",
            "whatsapp_enabled": False,
            "email_enabled": False,
            "google_calendar_enabled": False,
            "whatsapp_number": "",
            "resend_from_email": "",
            "auto_assign_enabled": True,
            "auto_followup_enabled": True,
            "auto_call_on_new_lead": False,
            "missed_call_followup_enabled": True,
            "missed_call_followup_hours": 24,
        }
        await db.settings.insert_one(s.copy())
    s.pop("_id", None)
    return s


@api.patch("/settings")
async def update_settings(body: SettingsBody, actor: dict = Depends(require_roles("admin"))):
    update = body.model_dump(exclude_none=True)
    await db.settings.update_one({"id": "singleton"}, {"$set": update}, upsert=True)
    s = await db.settings.find_one({"id": "singleton"}, {"_id": 0})
    return s


# ---------------------------------------------------------------------------
# CALL / SMS / EMAIL LOGGING (with recording URL for calls)
# ---------------------------------------------------------------------------
class CallLogBody(BaseDoc):
    direction: Literal["outgoing", "incoming"] = "outgoing"
    duration_sec: int = 0
    disposition: Literal["connected", "missed", "busy", "no_answer", "voicemail"] = "connected"
    recording_url: Optional[str] = None
    notes: Optional[str] = None


class SmsLogBody(BaseDoc):
    text: str
    template_id: Optional[str] = None


class EmailLogBody(BaseDoc):
    subject: str
    body: str
    template_id: Optional[str] = None


@api.post("/leads/{lead_id}/log-call")
async def log_call(lead_id: str, body: CallLogBody, actor: dict = Depends(get_current_user)):
    lead = await db.leads.find_one({"id": lead_id})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    meta = body.model_dump()
    kind = "outgoing_call" if body.direction == "outgoing" else "incoming_call"
    if body.disposition in ("missed", "busy", "no_answer"):
        kind = "missed_call"
    msg = f"{body.direction.capitalize()} call · {body.disposition} · {body.duration_sec}s"
    await log_activity(lead_id, actor, kind, msg, meta)
    return {"ok": True}


@api.post("/leads/{lead_id}/log-sms")
async def log_sms(lead_id: str, body: SmsLogBody, actor: dict = Depends(get_current_user)):
    lead = await db.leads.find_one({"id": lead_id})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    await log_activity(lead_id, actor, "sms_sent", body.text[:200], body.model_dump())
    return {"ok": True}


@api.post("/leads/{lead_id}/log-email")
async def log_email(lead_id: str, body: EmailLogBody, actor: dict = Depends(get_current_user)):
    lead = await db.leads.find_one({"id": lead_id})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    await log_activity(lead_id, actor, "email_sent", body.subject, body.model_dump())
    return {"ok": True}


# ---------------------------------------------------------------------------
# BULK LEAD ALLOCATION
# ---------------------------------------------------------------------------
class BulkAssignBody(BaseDoc):
    lead_ids: List[str]
    user_id: str


@api.post("/leads/bulk-assign")
async def bulk_assign(body: BulkAssignBody, actor: dict = Depends(require_roles("admin", "manager"))):
    if not body.lead_ids:
        raise HTTPException(status_code=400, detail="No leads provided")
    user = await db.users.find_one({"id": body.user_id}, {"name": 1})
    if not user:
        raise HTTPException(status_code=404, detail="Assignee not found")
    result = await db.leads.update_many(
        {"id": {"$in": body.lead_ids}},
        {"$set": {"assigned_to": body.user_id, "updated_at": now_utc().isoformat()}},
    )
    for lid in body.lead_ids:
        await log_activity(lid, actor, "assignment", f"Bulk-assigned to {user.get('name')}")
    return {"ok": True, "matched": result.matched_count, "modified": result.modified_count}


# ---------------------------------------------------------------------------
# CSV IMPORT
# ---------------------------------------------------------------------------
class ImportRow(BaseDoc):
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    source: Optional[str] = "manual"
    project_name: Optional[str] = None
    budget_min: Optional[float] = None
    budget_max: Optional[float] = None
    configuration: Optional[str] = None
    location_pref: Optional[str] = None
    notes: Optional[str] = None


class ImportBody(BaseDoc):
    rows: List[ImportRow]
    auto_assign: bool = True


@api.post("/leads/import")
async def import_leads(body: ImportBody, actor: dict = Depends(require_roles("admin", "manager"))):
    projects = {p["name"].lower(): p["id"] async for p in db.projects.find({}, {"id": 1, "name": 1})}
    created, failed = 0, 0
    for row in body.rows:
        try:
            pid = projects.get((row.project_name or "").lower())
            doc = {
                "id": new_id(),
                "name": row.name,
                "phone": row.phone,
                "email": row.email,
                "source": row.source if row.source in ("magicbricks", "99acres", "commonfloor", "housing", "website", "google_ads", "facebook", "instagram", "referral", "walk_in", "manual") else "manual",
                "project_id": pid,
                "budget_min": row.budget_min,
                "budget_max": row.budget_max,
                "configuration": row.configuration,
                "location_pref": row.location_pref,
                "notes": row.notes,
                "stage": "new",
                "priority": "warm",
                "stars": 0,
                "created_at": now_utc().isoformat(),
                "updated_at": now_utc().isoformat(),
            }
            if body.auto_assign:
                doc["assigned_to"] = await _pick_auto_assignee()
            await db.leads.insert_one(doc)
            await log_activity(doc["id"], actor, "lead_created", "Imported via CSV")
            created += 1
        except Exception:
            failed += 1
    return {"created": created, "failed": failed}


# ---------------------------------------------------------------------------
# WHATSAPP TEMPLATES
# ---------------------------------------------------------------------------
class WATemplateBody(BaseDoc):
    name: str
    category: Optional[str] = "general"
    body: str
    variables: Optional[List[str]] = None
    approved: Optional[bool] = False


@api.get("/whatsapp-templates")
async def list_wa_templates(user: dict = Depends(get_current_user)):
    docs = await db.whatsapp_templates.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)
    return docs


@api.post("/whatsapp-templates")
async def create_wa_template(body: WATemplateBody, actor: dict = Depends(require_roles("admin", "manager"))):
    doc = body.model_dump()
    doc["id"] = new_id()
    doc["created_at"] = now_utc().isoformat()
    await db.whatsapp_templates.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api.patch("/whatsapp-templates/{tid}")
async def update_wa_template(tid: str, body: WATemplateBody, actor: dict = Depends(require_roles("admin", "manager"))):
    r = await db.whatsapp_templates.update_one({"id": tid}, {"$set": body.model_dump(exclude_none=True)})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Template not found")
    return await db.whatsapp_templates.find_one({"id": tid}, {"_id": 0})


@api.delete("/whatsapp-templates/{tid}")
async def delete_wa_template(tid: str, actor: dict = Depends(require_roles("admin", "manager"))):
    await db.whatsapp_templates.delete_one({"id": tid})
    return {"ok": True}


# ---------------------------------------------------------------------------
# CHANNEL PARTNERS
# ---------------------------------------------------------------------------
class ChannelPartnerBody(BaseDoc):
    name: str
    company: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    city: Optional[str] = None
    rera: Optional[str] = None
    commission_pct: Optional[float] = None
    active: Optional[bool] = True
    notes: Optional[str] = None


@api.get("/channel-partners")
async def list_partners(user: dict = Depends(get_current_user)):
    docs = await db.channel_partners.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)
    for d in docs:
        d["leads_count"] = await db.leads.count_documents({"channel_partner_id": d["id"]})
    return docs


@api.post("/channel-partners")
async def create_partner(body: ChannelPartnerBody, actor: dict = Depends(require_roles("admin", "manager"))):
    doc = body.model_dump()
    doc["id"] = new_id()
    doc["created_at"] = now_utc().isoformat()
    await db.channel_partners.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api.patch("/channel-partners/{pid}")
async def update_partner(pid: str, body: ChannelPartnerBody, actor: dict = Depends(require_roles("admin", "manager"))):
    r = await db.channel_partners.update_one({"id": pid}, {"$set": body.model_dump(exclude_none=True)})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Partner not found")
    return await db.channel_partners.find_one({"id": pid}, {"_id": 0})


@api.delete("/channel-partners/{pid}")
async def delete_partner(pid: str, actor: dict = Depends(require_roles("admin"))):
    await db.channel_partners.delete_one({"id": pid})
    return {"ok": True}


# ---------------------------------------------------------------------------
# PROPOSALS
# ---------------------------------------------------------------------------
class ProposalBody(BaseDoc):
    lead_id: str
    project_id: Optional[str] = None
    unit_id: Optional[str] = None
    amount: float
    validity_days: Optional[int] = 15
    status: Optional[Literal["draft", "sent", "accepted", "declined", "expired"]] = "draft"
    terms: Optional[str] = None


class UpdateProposalBody(BaseDoc):
    amount: Optional[float] = None
    validity_days: Optional[int] = None
    status: Optional[Literal["draft", "sent", "accepted", "declined", "expired"]] = None
    terms: Optional[str] = None


@api.get("/proposals")
async def list_proposals(lead_id: Optional[str] = None, user: dict = Depends(get_current_user)):
    q = {}
    if lead_id:
        q["lead_id"] = lead_id
    docs = await db.proposals.find(q, {"_id": 0}).sort("created_at", -1).to_list(500)
    return docs


@api.post("/proposals")
async def create_proposal(body: ProposalBody, actor: dict = Depends(get_current_user)):
    doc = body.model_dump()
    doc["id"] = new_id()
    doc["created_at"] = now_utc().isoformat()
    doc["created_by"] = actor["id"]
    await db.proposals.insert_one(doc)
    doc.pop("_id", None)
    await log_activity(body.lead_id, actor, "proposal_created", f"Proposal for ₹{int(body.amount)} created")
    return doc


@api.patch("/proposals/{prop_id}")
async def update_proposal(prop_id: str, body: UpdateProposalBody, actor: dict = Depends(get_current_user)):
    update = body.model_dump(exclude_none=True)
    r = await db.proposals.update_one({"id": prop_id}, {"$set": update})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Proposal not found")
    prop = await db.proposals.find_one({"id": prop_id}, {"_id": 0})
    if "status" in update:
        await log_activity(prop["lead_id"], actor, "proposal_" + update["status"], f"Proposal marked {update['status']}")
    return prop


@api.delete("/proposals/{prop_id}")
async def delete_proposal(prop_id: str, actor: dict = Depends(require_roles("admin", "manager"))):
    await db.proposals.delete_one({"id": prop_id})
    return {"ok": True}


# ---------------------------------------------------------------------------
# DASHBOARD (Month's Updates + Action Items)
# ---------------------------------------------------------------------------
def _month_bounds(dt: datetime) -> tuple:
    start = datetime(dt.year, dt.month, 1, tzinfo=timezone.utc)
    if dt.month == 12:
        end = datetime(dt.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(dt.year, dt.month + 1, 1, tzinfo=timezone.utc)
    return start, end


@api.get("/dashboard/monthly")
async def dashboard_monthly(user: dict = Depends(get_current_user)):
    """Month's Updates tab data."""
    scope: dict = {}
    if user["role"] == "executive":
        scope["assigned_to"] = user["id"]

    now = now_utc()
    cur_start, cur_end = _month_bounds(now)
    prev_dt = cur_start - timedelta(days=1)
    prev_start, prev_end = _month_bounds(prev_dt)

    def iso(d):
        return d.isoformat()

    # KPIs current month
    new_leads = await db.leads.count_documents({**scope, "created_at": {"$gte": iso(cur_start), "$lt": iso(cur_end)}})
    booked = await db.leads.count_documents({**scope, "stage": "booked", "updated_at": {"$gte": iso(cur_start), "$lt": iso(cur_end)}})
    # revenue = sum of accepted proposals in month
    cur_rev_pipe = [
        {"$match": {"status": "accepted", "created_at": {"$gte": iso(cur_start), "$lt": iso(cur_end)}}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    cur_rev_docs = [r async for r in db.proposals.aggregate(cur_rev_pipe)]
    cur_revenue = cur_rev_docs[0]["total"] if cur_rev_docs else 0

    prev_rev_pipe = [
        {"$match": {"status": "accepted", "created_at": {"$gte": iso(prev_start), "$lt": iso(prev_end)}}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    prev_rev_docs = [r async for r in db.proposals.aggregate(prev_rev_pipe)]
    prev_revenue = prev_rev_docs[0]["total"] if prev_rev_docs else 0

    total_leads_m = new_leads
    conversion = round((booked / total_leads_m) * 100, 2) if total_leads_m > 0 else 0.0

    revenue_pct = 0
    if prev_revenue > 0:
        revenue_pct = round(((cur_revenue - prev_revenue) / prev_revenue) * 100, 2)

    # Telemetry — last 4 months by activity kind
    telemetry = []
    for i in range(3, -1, -1):
        month_dt = (cur_start.replace(day=15) - timedelta(days=30 * i))
        ms, me = _month_bounds(month_dt)
        row = {"label": ms.strftime("%b-%y"), "month": ms.strftime("%Y-%m")}
        for kind_key, kinds in (
            ("outgoing_call", ["outgoing_call", "call"]),
            ("email_sent", ["email_sent", "email"]),
            ("sms_sent", ["sms_sent"]),
            ("followup_scheduled", ["followup_scheduled"]),
        ):
            row[kind_key] = await db.activities.count_documents({
                "kind": {"$in": kinds},
                "created_at": {"$gte": iso(ms), "$lt": iso(me)},
            })
        telemetry.append(row)

    # Total lead pipeline (bar) — counts by stage across all-time in scope
    stages = ["new", "contacted", "qualified", "site_visit", "negotiation", "booked", "lost"]
    pipeline = []
    for s in stages:
        pipeline.append({"stage": s, "count": await db.leads.count_documents({**scope, "stage": s})})

    # Upcoming closures — proposals in draft/sent with validity ending soon OR leads in negotiation
    closures_raw = await db.leads.find({**scope, "stage": {"$in": ["negotiation", "site_visit"]}}, {"_id": 0}).sort("updated_at", -1).limit(8).to_list(8)

    # Top leads — sorted by stars desc then updated_at
    top = await db.leads.find({**scope, "stars": {"$gt": 0}}, {"_id": 0}).sort([("stars", -1), ("updated_at", -1)]).limit(6).to_list(6)
    if len(top) < 6:
        rest = await db.leads.find({**scope, "stage": {"$nin": ["lost", "booked"]}}, {"_id": 0}).sort("updated_at", -1).limit(6 - len(top)).to_list(6)
        top = top + [r for r in rest if r["id"] not in {t["id"] for t in top}]

    # Recent inquiries — latest 8 leads
    recent = await db.leads.find(scope, {"_id": 0}).sort("created_at", -1).limit(8).to_list(8)

    return {
        "period": {"start": iso(cur_start), "end": iso(cur_end)},
        "kpi": {"new_leads": new_leads, "revenue": cur_revenue, "booked": booked, "conversion": conversion},
        "revenue": {"current": cur_revenue, "previous": prev_revenue, "change_pct": revenue_pct},
        "telemetry": telemetry,
        "pipeline": pipeline,
        "top_leads": top,
        "recent_inquiries": recent,
        "upcoming_closures": closures_raw,
    }


@api.get("/dashboard/action-items")
async def dashboard_action_items(user: dict = Depends(get_current_user)):
    scope: dict = {}
    if user["role"] == "executive":
        scope["assigned_to"] = user["id"]

    now = now_utc()
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    def iso(d):
        return d.isoformat()

    # Missed calls today (activity kind = missed_call)
    missed = await db.activities.count_documents({
        "kind": "missed_call",
        "created_at": {"$gte": iso(day_start), "$lt": iso(day_end)},
    })

    todays_followups = await db.follow_ups.find({
        **scope,
        "status": "pending",
        "due_at": {"$gte": iso(day_start), "$lt": iso(day_end)},
    }, {"_id": 0}).sort("due_at", 1).to_list(100)

    scheduled_calls = await db.follow_ups.count_documents({**scope, "status": "pending", "kind": "call"})
    tasks = await db.follow_ups.count_documents({**scope, "status": "pending", "kind": {"$in": ["meeting", "email", "whatsapp"]}})

    planned_visits = await db.site_visits.find({
        **scope,
        "status": "scheduled",
        "scheduled_at": {"$gte": iso(day_start - timedelta(days=1)), "$lt": iso(day_start + timedelta(days=7))},
    }, {"_id": 0}).sort("scheduled_at", 1).to_list(100)

    # Leads with no calls done — leads with no activities of call kind
    lead_docs = await db.leads.find({**scope, "stage": {"$nin": ["booked", "lost"]}}, {"_id": 0}).sort("created_at", -1).limit(200).to_list(200)
    lead_ids = [l["id"] for l in lead_docs]
    called_ids = set()
    if lead_ids:
        pipe = [
            {"$match": {"lead_id": {"$in": lead_ids}, "kind": {"$in": ["outgoing_call", "call", "incoming_call", "missed_call"]}}},
            {"$group": {"_id": "$lead_id"}},
        ]
        called_ids = {r["_id"] async for r in db.activities.aggregate(pipe)}
    no_call_leads = [l for l in lead_docs if l["id"] not in called_ids][:12]

    # No follow-up added leads
    followed_ids = set()
    if lead_ids:
        followed = await db.follow_ups.find({"lead_id": {"$in": lead_ids}}, {"lead_id": 1}).to_list(len(lead_ids))
        followed_ids = {f["lead_id"] for f in followed}
    no_followup_leads = [l for l in lead_docs if l["id"] not in followed_ids][:12]

    return {
        "period": {"start": iso(day_start), "end": iso(day_end)},
        "widgets": {
            "missed_calls": missed,
            "todays_followups": len(todays_followups),
            "scheduled_calls": scheduled_calls,
            "tasks": tasks,
        },
        "todays_followups": todays_followups,
        "planned_visits": planned_visits,
        "no_call_leads": no_call_leads,
        "no_followup_leads": no_followup_leads,
    }


# ---------------------------------------------------------------------------
# REPORTS (aggregations)
# ---------------------------------------------------------------------------
@api.get("/reports/executives")
async def report_executives(user: dict = Depends(get_current_user)):
    execs = await db.users.find({"role": "executive"}, {"_id": 0, "password_hash": 0}).to_list(100)
    rows = []
    for e in execs:
        total = await db.leads.count_documents({"assigned_to": e["id"]})
        booked = await db.leads.count_documents({"assigned_to": e["id"], "stage": "booked"})
        site_visits = await db.site_visits.count_documents({"assigned_to": e["id"]})
        pending = await db.follow_ups.count_documents({"assigned_to": e["id"], "status": "pending"})
        conv = round((booked / total * 100), 1) if total else 0
        rows.append({
            "id": e["id"], "name": e["name"], "email": e["email"],
            "leads": total, "booked": booked, "site_visits": site_visits,
            "pending_followups": pending, "conversion": conv,
        })
    rows.sort(key=lambda r: -r["conversion"])
    return rows


@api.get("/reports/sources")
async def report_sources(user: dict = Depends(get_current_user)):
    pipe = [{"$group": {"_id": {"source": "$source", "stage": "$stage"}, "count": {"$sum": 1}}}]
    rows = {}
    async for r in db.leads.aggregate(pipe):
        src = r["_id"]["source"]
        stg = r["_id"]["stage"]
        rows.setdefault(src, {"source": src, "total": 0, "booked": 0})
        rows[src]["total"] += r["count"]
        if stg == "booked":
            rows[src]["booked"] += r["count"]
    out = list(rows.values())
    for o in out:
        o["conversion"] = round((o["booked"] / o["total"] * 100), 1) if o["total"] else 0
    out.sort(key=lambda r: -r["total"])
    return out



# ---------------------------------------------------------------------------
# TWILIO VOICE — click-to-call, status + recording callbacks
# ---------------------------------------------------------------------------
from twilio.rest import Client as TwilioClient  # noqa: E402
from twilio.request_validator import RequestValidator  # noqa: E402
from twilio.twiml.voice_response import VoiceResponse  # noqa: E402
from fastapi.responses import Response as StarletteResponse  # noqa: E402

TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER")
BACKEND_PUBLIC_URL = os.environ.get("BACKEND_PUBLIC_URL", "")

_twilio_client: Optional[TwilioClient] = None
_twilio_validator: Optional[RequestValidator] = None
if TWILIO_SID and TWILIO_TOKEN:
    try:
        _twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        _twilio_validator = RequestValidator(TWILIO_TOKEN)
        log.info("Twilio client initialised (from=%s)", TWILIO_FROM)
    except Exception as e:
        log.warning("Twilio init failed: %s", e)


def _looks_like_e164(num: str) -> bool:
    return bool(num) and num.startswith("+") and len(num) >= 8


async def _initiate_twilio_call(lead: dict, actor: dict) -> dict:
    """Executive-first bridged call via Twilio. Rings the executive; on answer
    the TwiML dials the lead and records the conversation."""
    if not lead.get("phone") or not _looks_like_e164(lead["phone"].replace(" ", "")):
        raise HTTPException(status_code=400, detail="Lead has no valid E.164 phone")
    exec_phone = (actor.get("phone") or "").replace(" ", "")
    if not _looks_like_e164(exec_phone):
        raise HTTPException(status_code=400, detail="Set your phone (E.164, e.g. +9198…) on the Team page first")

    lead_phone = lead["phone"].replace(" ", "")
    activity_id = new_id()
    now = now_utc().isoformat()

    if not _twilio_client or not TWILIO_FROM or not BACKEND_PUBLIC_URL:
        await db.activities.insert_one({
            "id": activity_id,
            "lead_id": lead["id"],
            "actor_id": actor.get("id"),
            "actor_name": actor.get("name") or "system",
            "kind": "outgoing_call",
            "message": f"[MOCK] Call queued to {lead['name']}",
            "meta": {"direction": "outgoing", "status": "queued", "mock": True, "to": lead_phone, "from": exec_phone},
            "created_at": now,
        })
        return {"call_sid": None, "status": "mock", "mock": True, "activity_id": activity_id}

    twiml_url = f"{BACKEND_PUBLIC_URL}/api/twilio/twiml/{lead['id']}"
    status_cb = f"{BACKEND_PUBLIC_URL}/api/twilio/status-callback"
    recording_cb = f"{BACKEND_PUBLIC_URL}/api/twilio/recording-callback"

    try:
        call = _twilio_client.calls.create(
            to=exec_phone,
            from_=TWILIO_FROM,
            url=twiml_url,
            method="POST",
            status_callback=status_cb,
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
            record=True,
            recording_status_callback=recording_cb,
            recording_status_callback_method="POST",
        )
    except Exception as e:
        # Return HTTP 400 (not 502) so the preview gateway does not rewrite the JSON body.
        raise HTTPException(status_code=400, detail=f"Twilio error: {e}")

    await db.activities.insert_one({
        "id": activity_id,
        "lead_id": lead["id"],
        "actor_id": actor.get("id"),
        "actor_name": actor.get("name") or "system",
        "kind": "outgoing_call",
        "message": f"Call initiated to {lead['name']}",
        "meta": {
            "direction": "outgoing",
            "call_sid": call.sid,
            "status": call.status,
            "to_lead": lead_phone,
            "to_exec": exec_phone,
        },
        "created_at": now,
    })
    return {"call_sid": call.sid, "status": call.status, "mock": False, "activity_id": activity_id}


@api.post("/leads/{lead_id}/call")
async def initiate_call(lead_id: str, actor: dict = Depends(get_current_user)):
    lead = await db.leads.find_one({"id": lead_id})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    if actor["role"] == "executive" and lead.get("assigned_to") != actor["id"]:
        raise HTTPException(status_code=403, detail="You can only call your own leads")
    return await _initiate_twilio_call(lead, actor)


@api.api_route("/twilio/twiml/{lead_id}", methods=["GET", "POST"])
async def twilio_twiml(lead_id: str, request: Request):
    lead = await db.leads.find_one({"id": lead_id})
    resp = VoiceResponse()
    if not lead or not lead.get("phone"):
        resp.say("Sorry, this lead has no phone number on record. Goodbye.")
        return StarletteResponse(content=str(resp), media_type="application/xml")

    resp.say(f"Connecting you to {lead.get('name', 'the lead')}.", voice="Polly.Aditi", language="en-IN")
    recording_cb = f"{BACKEND_PUBLIC_URL}/api/twilio/recording-callback" if BACKEND_PUBLIC_URL else None
    dial_kwargs = {"record": "record-from-answer-dual", "caller_id": TWILIO_FROM, "timeout": 30}
    if recording_cb:
        dial_kwargs["recording_status_callback"] = recording_cb
        dial_kwargs["recording_status_callback_method"] = "POST"
    dial = resp.dial(**dial_kwargs)
    dial.number(lead["phone"].replace(" ", ""))
    return StarletteResponse(content=str(resp), media_type="application/xml")


def _twilio_verify(request: Request, form_dict: dict) -> bool:
    if not _twilio_validator:
        return True
    sig = request.headers.get("X-Twilio-Signature", "")
    # Rebuild URL against BACKEND_PUBLIC_URL so signature matches what Twilio signed,
    # regardless of ingress / proxy stripping the scheme/host.
    if BACKEND_PUBLIC_URL:
        url = BACKEND_PUBLIC_URL.rstrip("/") + request.url.path
        if request.url.query:
            url += "?" + request.url.query
    else:
        url = str(request.url)
    return _twilio_validator.validate(url, form_dict, sig)


@api.post("/twilio/status-callback")
async def twilio_status_callback(request: Request):
    form = dict(await request.form())
    if not _twilio_verify(request, form):
        log.warning("Twilio signature mismatch on status-callback")
        return StarletteResponse(status_code=403)

    call_sid = form.get("CallSid")
    parent_sid = form.get("ParentCallSid")
    key_sid = parent_sid or call_sid
    call_status = form.get("CallStatus") or ""
    duration = form.get("CallDuration")

    act = await db.activities.find_one({"meta.call_sid": key_sid})
    if not act:
        return StarletteResponse(status_code=200)

    meta = act.get("meta") or {}
    meta["status"] = call_status
    try:
        if duration:
            meta["duration_sec"] = int(duration)
    except ValueError:
        pass

    unsuccessful = {"no-answer", "busy", "failed", "canceled"}
    dur_i = int(meta.get("duration_sec") or 0)
    is_missed = call_status in unsuccessful or (call_status == "completed" and dur_i == 0)
    disposition = call_status.replace("-", "_") if is_missed else "connected"
    meta["disposition"] = disposition
    kind = "missed_call" if is_missed else "outgoing_call"

    await db.activities.update_one(
        {"id": act["id"]},
        {"$set": {"meta": meta, "kind": kind, "message": f"Call {call_status} · {duration or 0}s"}},
    )

    if is_missed:
        settings = await db.settings.find_one({"id": "singleton"}) or {}
        # notify the executive/manager of missed call
        lead = await db.leads.find_one({"id": act["lead_id"]}, {"name": 1, "assigned_to": 1, "_id": 0})
        if lead and lead.get("assigned_to"):
            await create_notification(
                type="missed_call",
                title="Missed call",
                message=f"{lead.get('name', 'Lead')} did not connect ({call_status})",
                user_id=lead["assigned_to"],
                link=f"/leads/{act['lead_id']}",
                meta={"lead_id": act["lead_id"], "call_sid": key_sid},
            )
        if settings.get("missed_call_followup_enabled", True):
            hours = float(settings.get("missed_call_followup_hours", 24))
            due = now_utc() + timedelta(hours=hours)
            await db.follow_ups.insert_one({
                "id": new_id(),
                "lead_id": act["lead_id"],
                "due_at": due.isoformat(),
                "kind": "call",
                "notes": f"Auto-scheduled after {call_status} call",
                "assigned_to": act.get("actor_id") or (await db.leads.find_one({"id": act["lead_id"]}, {"assigned_to": 1}) or {}).get("assigned_to"),
                "status": "pending",
                "created_at": now_utc().isoformat(),
                "meta": {"auto": True, "source_call_sid": key_sid},
            })
    return StarletteResponse(status_code=200)


@api.post("/twilio/recording-callback")
async def twilio_recording_callback(request: Request):
    form = dict(await request.form())
    if not _twilio_verify(request, form):
        log.warning("Twilio signature mismatch on recording-callback")
        return StarletteResponse(status_code=403)

    call_sid = form.get("CallSid")
    parent_sid = form.get("ParentCallSid")
    key_sid = parent_sid or call_sid
    recording_url = form.get("RecordingUrl")
    recording_sid = form.get("RecordingSid")
    duration = form.get("RecordingDuration")
    if recording_url and not recording_url.lower().endswith((".mp3", ".wav")):
        recording_url = recording_url + ".mp3"

    act = await db.activities.find_one({"meta.call_sid": key_sid})
    if act:
        meta = act.get("meta") or {}
        meta["recording_url"] = recording_url
        meta["recording_sid"] = recording_sid
        try:
            meta["recording_duration_sec"] = int(duration or 0)
        except ValueError:
            pass
        await db.activities.update_one({"id": act["id"]}, {"$set": {"meta": meta}})
    return StarletteResponse(status_code=200)


@api.get("/twilio/status")
async def twilio_status(user: dict = Depends(get_current_user)):
    return {
        "configured": bool(_twilio_client),
        "from_number": TWILIO_FROM if _twilio_client else None,
        "webhook_base": BACKEND_PUBLIC_URL,
    }



# ---------------------------------------------------------------------------
# DASHBOARD DRILL-DOWNS
# ---------------------------------------------------------------------------
@api.get("/dashboard/revenue-breakdown")
async def dashboard_revenue_breakdown(user: dict = Depends(get_current_user)):
    """Agent-wise + project-wise revenue split (accepted proposals, current month).
    Executives see only their own attributions."""
    now = now_utc()
    cur_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    if now.month == 12:
        cur_end = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        cur_end = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)

    match: dict = {"status": "accepted", "created_at": {"$gte": cur_start.isoformat(), "$lt": cur_end.isoformat()}}
    if user["role"] == "executive":
        match["created_by"] = user["id"]

    # by agent
    by_agent_pipe = [
        {"$match": match},
        {"$group": {"_id": "$created_by", "amount": {"$sum": "$amount"}, "count": {"$sum": 1}}},
    ]
    by_agent = []
    async for r in db.proposals.aggregate(by_agent_pipe):
        u = await db.users.find_one({"id": r["_id"]}, {"name": 1, "_id": 0}) if r["_id"] else None
        by_agent.append({
            "id": r["_id"], "name": (u or {}).get("name") or "Unassigned",
            "amount": r["amount"], "count": r["count"],
        })
    by_agent.sort(key=lambda r: -r["amount"])

    # by project
    by_project_pipe = [
        {"$match": match},
        {"$group": {"_id": "$project_id", "amount": {"$sum": "$amount"}, "count": {"$sum": 1}}},
    ]
    by_project = []
    async for r in db.proposals.aggregate(by_project_pipe):
        p = await db.projects.find_one({"id": r["_id"]}, {"name": 1, "_id": 0}) if r["_id"] else None
        by_project.append({
            "id": r["_id"], "name": (p or {}).get("name") or "Unassigned",
            "amount": r["amount"], "count": r["count"],
        })
    by_project.sort(key=lambda r: -r["amount"])

    total = sum(a["amount"] for a in by_agent)
    return {"total": total, "by_agent": by_agent, "by_project": by_project, "period": {"start": cur_start.isoformat(), "end": cur_end.isoformat()}}


# ---------------------------------------------------------------------------
# INVENTORY IMPORT / EXPORT
# ---------------------------------------------------------------------------
@api.get("/units/export")
async def export_units(project_id: Optional[str] = None, user: dict = Depends(get_current_user)):
    q = {}
    if project_id:
        q["project_id"] = project_id
    units = await db.units.find(q, {"_id": 0}).sort([("tower", 1), ("floor", 1), ("unit_no", 1)]).to_list(5000)
    header = "tower,floor,unit_no,config,carpet_area,price,facing,status\n"
    lines = [header]
    for u in units:
        line = ",".join([
            str(u.get("tower", "")),
            str(u.get("floor", "")),
            str(u.get("unit_no", "")),
            str(u.get("config", "")),
            str(u.get("carpet_area", "")),
            str(int(u.get("price") or 0)),
            str(u.get("facing", "")),
            str(u.get("status", "")),
        ])
        lines.append(line + "\n")
    body = "".join(lines)
    fname = f"tasko-units-{project_id or 'all'}.csv"
    return StarletteResponse(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


class UnitImportRow(BaseDoc):
    tower: str
    floor: int
    unit_no: str
    config: str
    carpet_area: Optional[float] = None
    price: Optional[float] = None
    facing: Optional[str] = None
    status: Optional[UnitStatus] = "available"


class UnitImportBody(BaseDoc):
    project_id: str
    rows: List[UnitImportRow]
    replace_existing: bool = False


@api.post("/units/import")
async def import_units(body: UnitImportBody, actor: dict = Depends(require_roles("admin", "manager"))):
    project = await db.projects.find_one({"id": body.project_id})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if body.replace_existing:
        await db.units.delete_many({"project_id": body.project_id})
    created, failed = 0, 0
    for r in body.rows:
        try:
            doc = r.model_dump()
            doc["id"] = new_id()
            doc["project_id"] = body.project_id
            doc["created_at"] = now_utc().isoformat()
            await db.units.insert_one(doc)
            created += 1
        except Exception:
            failed += 1
    return {"created": created, "failed": failed, "replaced": body.replace_existing}



# ---------------------------------------------------------------------------
# NOTIFICATIONS — role-scoped in-app alerts
# ---------------------------------------------------------------------------
NOTIF_TYPES = Literal[
    "lead_assigned", "followup_due", "sitevisit_reminder", "missed_call",
    "team_overdue", "stale_lead", "negotiation_pending", "exec_no_activity",
    "eod_summary",
]


class NotificationBody(BaseDoc):
    id: Optional[str] = None
    user_id: Optional[str] = None
    role_scope: Optional[str] = None  # "admin" | "manager" | "executive" | None
    type: str
    title: str
    message: str
    link: Optional[str] = None
    meta: Optional[dict] = None


async def create_notification(
    *,
    type: str,
    title: str,
    message: str,
    user_id: Optional[str] = None,
    role_scope: Optional[str] = None,
    link: Optional[str] = None,
    meta: Optional[dict] = None,
    dedupe_key: Optional[str] = None,
) -> None:
    """Insert a notification. If dedupe_key given, skip if one already exists
    for the same target and key in the last 24h."""
    if dedupe_key:
        since = (now_utc() - timedelta(hours=24)).isoformat()
        exists = await db.notifications.find_one({
            "dedupe_key": dedupe_key,
            "user_id": user_id,
            "role_scope": role_scope,
            "created_at": {"$gte": since},
        })
        if exists:
            return
    doc = {
        "id": new_id(),
        "user_id": user_id,
        "role_scope": role_scope,
        "type": type,
        "title": title,
        "message": message,
        "link": link,
        "meta": meta or {},
        "read": False,
        "dedupe_key": dedupe_key,
        "created_at": now_utc().isoformat(),
    }
    await db.notifications.insert_one(doc)


def _notif_scope_filter(user: dict) -> dict:
    """Admin sees admin + manager notifs (and personal). Manager sees manager notifs (and personal). Executive only personal."""
    role = user.get("role")
    scopes = []
    if role == "admin":
        scopes = ["admin", "manager"]
    elif role == "manager":
        scopes = ["manager"]
    ors: list = [{"user_id": user["id"]}]
    if scopes:
        ors.append({"role_scope": {"$in": scopes}})
    return {"$or": ors}


@api.get("/notifications")
async def list_notifications(unread_only: bool = False, user: dict = Depends(get_current_user)):
    q = _notif_scope_filter(user)
    if unread_only:
        q = {**q, "read": False}
    docs = await db.notifications.find(q, {"_id": 0}).sort("created_at", -1).limit(50).to_list(50)
    unread = await db.notifications.count_documents({**q, "read": False})
    return {"items": docs, "unread": unread}


@api.post("/notifications/{nid}/read")
async def mark_read(nid: str, user: dict = Depends(get_current_user)):
    scope = _notif_scope_filter(user)
    await db.notifications.update_one({"id": nid, **scope}, {"$set": {"read": True}})
    return {"ok": True}


@api.post("/notifications/read-all")
async def mark_all_read(user: dict = Depends(get_current_user)):
    scope = _notif_scope_filter(user)
    r = await db.notifications.update_many({**scope, "read": False}, {"$set": {"read": True}})
    return {"ok": True, "updated": r.modified_count}


async def refresh_notifications() -> dict:
    """Compute time-sensitive notifications: due follow-ups, upcoming visits,
    stale leads, team overdue, negotiation pending, exec inactivity."""
    now = now_utc()
    today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    today_end = today_start + timedelta(days=1)
    stale_cutoff = (now - timedelta(days=3)).isoformat()

    created = 0

    # Agent-level: follow-ups due today / overdue
    fus = await db.follow_ups.find({
        "status": "pending",
        "due_at": {"$lt": today_end.isoformat()},
        "assigned_to": {"$ne": None},
    }, {"_id": 0}).to_list(500)
    for f in fus:
        due = f.get("due_at", "")
        overdue = due < now.isoformat()
        dedupe = f"fu:{f['id']}:{today_start.date().isoformat()}"
        lead = await db.leads.find_one({"id": f.get("lead_id")}, {"name": 1, "_id": 0})
        title = "Follow-up overdue" if overdue else "Follow-up due today"
        msg = f"{f.get('kind', 'call').title()} · {(lead or {}).get('name', 'Lead')}"
        await create_notification(
            type="followup_due",
            title=title,
            message=msg,
            user_id=f["assigned_to"],
            link=f"/leads/{f['lead_id']}",
            meta={"followup_id": f["id"], "overdue": overdue},
            dedupe_key=dedupe,
        )
        created += 1

    # Agent-level: site visits within next 24h
    visits = await db.site_visits.find({
        "status": "scheduled",
        "scheduled_at": {"$gte": now.isoformat(), "$lt": (now + timedelta(hours=24)).isoformat()},
        "assigned_to": {"$ne": None},
    }, {"_id": 0}).to_list(200)
    for v in visits:
        dedupe = f"sv:{v['id']}"
        lead = await db.leads.find_one({"id": v.get("lead_id")}, {"name": 1, "_id": 0})
        when = datetime.fromisoformat(v["scheduled_at"]).astimezone(timezone.utc)
        await create_notification(
            type="sitevisit_reminder",
            title="Site visit tomorrow",
            message=f"{(lead or {}).get('name', 'Lead')} · {when.strftime('%d %b, %H:%M UTC')}",
            user_id=v["assigned_to"],
            link=f"/leads/{v['lead_id']}",
            meta={"visit_id": v["id"]},
            dedupe_key=dedupe,
        )
        created += 1

    # Manager-level: team overdue follow-ups (aggregate)
    overdue_count = await db.follow_ups.count_documents({
        "status": "pending",
        "due_at": {"$lt": now.isoformat()},
    })
    if overdue_count > 0:
        await create_notification(
            type="team_overdue",
            title="Team follow-ups overdue",
            message=f"{overdue_count} follow-up{'s' if overdue_count != 1 else ''} past due across the team",
            role_scope="manager",
            link="/follow-ups",
            meta={"count": overdue_count},
            dedupe_key=f"team_overdue:{today_start.date().isoformat()}",
        )
        created += 1

    # Manager-level: stale leads (no update in 3+ days, not booked/lost)
    stale = await db.leads.count_documents({
        "stage": {"$nin": ["booked", "lost"]},
        "updated_at": {"$lt": stale_cutoff},
    })
    if stale > 0:
        await create_notification(
            type="stale_lead",
            title="Stale leads",
            message=f"{stale} lead{'s' if stale != 1 else ''} with no activity for 3+ days",
            role_scope="manager",
            link="/leads",
            meta={"count": stale},
            dedupe_key=f"stale_leads:{today_start.date().isoformat()}",
        )
        created += 1

    # Manager-level: negotiation stage leads pending
    negotiation = await db.leads.count_documents({"stage": "negotiation"})
    if negotiation > 0:
        await create_notification(
            type="negotiation_pending",
            title="Deals in negotiation",
            message=f"{negotiation} lead{'s' if negotiation != 1 else ''} awaiting decision",
            role_scope="manager",
            link="/leads?stage=negotiation",
            meta={"count": negotiation},
            dedupe_key=f"negotiation:{today_start.date().isoformat()}",
        )
        created += 1

    # Manager-level: executives with 0 activity today
    execs = await db.users.find({"role": "executive", "active": {"$ne": False}}, {"_id": 0}).to_list(50)
    idle = []
    for e in execs:
        cnt = await db.activities.count_documents({
            "actor_id": e["id"],
            "created_at": {"$gte": today_start.isoformat(), "$lt": today_end.isoformat()},
        })
        if cnt == 0:
            idle.append(e["name"])
    if idle:
        await create_notification(
            type="exec_no_activity",
            title="Executives with no activity today",
            message=", ".join(idle[:5]) + (f" +{len(idle) - 5} more" if len(idle) > 5 else ""),
            role_scope="manager",
            link="/team",
            meta={"names": idle},
            dedupe_key=f"exec_idle:{today_start.date().isoformat()}",
        )
        created += 1

    return {"created": created}


@api.post("/notifications/refresh")
async def notif_refresh_endpoint(user: dict = Depends(get_current_user)):
    return await refresh_notifications()


# ---------------------------------------------------------------------------
# ADMIN EOD SUMMARY + RESEND EMAIL
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402
try:
    import resend  # noqa: E402
    resend.api_key = os.environ.get("RESEND_API_KEY", "")
except Exception:
    resend = None

SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "onboarding@resend.dev")
IST = timezone(timedelta(hours=5, minutes=30))


async def compute_eod_summary(target_date: Optional[datetime] = None) -> dict:
    """Aggregate today's key metrics for the admin."""
    now = target_date or now_utc()
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    s, e = day_start.isoformat(), day_end.isoformat()

    # Follow-ups due today
    fu_due = await db.follow_ups.count_documents({
        "status": "pending",
        "due_at": {"$gte": s, "$lt": e},
    })
    fu_overdue = await db.follow_ups.count_documents({
        "status": "pending",
        "due_at": {"$lt": now.isoformat()},
    })

    # Milestones today
    bookings_today = await db.leads.count_documents({
        "stage": "booked",
        "updated_at": {"$gte": s, "$lt": e},
    })
    site_visits_completed = await db.site_visits.count_documents({
        "status": "completed",
        "scheduled_at": {"$gte": s, "$lt": e},
    })
    new_leads = await db.leads.count_documents({"created_at": {"$gte": s, "$lt": e}})

    # Calls (from activities): total, connected, missed, total talk time
    call_kinds = ["outgoing_call", "incoming_call", "missed_call", "call"]
    calls_pipe = [
        {"$match": {"kind": {"$in": call_kinds}, "created_at": {"$gte": s, "$lt": e}}},
        {"$group": {
            "_id": None,
            "total": {"$sum": 1},
            "connected": {"$sum": {"$cond": [{"$eq": ["$meta.disposition", "connected"]}, 1, 0]}},
            "missed": {"$sum": {"$cond": [{"$eq": ["$kind", "missed_call"]}, 1, 0]}},
            "talk_time": {"$sum": {"$ifNull": ["$meta.duration_sec", 0]}},
        }},
    ]
    calls_row = None
    async for r in db.activities.aggregate(calls_pipe):
        calls_row = r
    calls = {
        "total": (calls_row or {}).get("total", 0),
        "connected": (calls_row or {}).get("connected", 0),
        "missed": (calls_row or {}).get("missed", 0),
        "talk_time_sec": int((calls_row or {}).get("talk_time", 0)),
    }

    # Top performers today (bookings / calls)
    pipe = [
        {"$match": {"kind": {"$in": call_kinds}, "created_at": {"$gte": s, "$lt": e}, "actor_id": {"$ne": None}}},
        {"$group": {"_id": "$actor_id", "calls": {"$sum": 1}, "talk_time": {"$sum": {"$ifNull": ["$meta.duration_sec", 0]}}}},
        {"$sort": {"calls": -1}}, {"$limit": 5},
    ]
    top_execs = []
    async for r in db.activities.aggregate(pipe):
        u = await db.users.find_one({"id": r["_id"]}, {"name": 1, "_id": 0})
        top_execs.append({
            "name": (u or {}).get("name", "Unknown"),
            "calls": r["calls"],
            "talk_time_sec": int(r["talk_time"] or 0),
        })

    return {
        "date": day_start.date().isoformat(),
        "generated_at": now_utc().isoformat(),
        "followups": {"due_today": fu_due, "overdue": fu_overdue},
        "milestones": {
            "bookings": bookings_today,
            "site_visits_completed": site_visits_completed,
            "new_leads": new_leads,
        },
        "calls": calls,
        "top_execs": top_execs,
    }


def _fmt_hms(sec: int) -> str:
    if sec <= 0:
        return "0m"
    h = sec // 3600
    m = (sec % 3600) // 60
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def _eod_html(summary: dict) -> str:
    c = summary["calls"]
    m = summary["milestones"]
    f = summary["followups"]
    rows_html = ""
    for t in summary.get("top_execs", []):
        rows_html += f"<tr><td style='padding:8px 12px;border-top:1px solid #E6E4DD;'>{t['name']}</td><td style='padding:8px 12px;border-top:1px solid #E6E4DD;text-align:right;'>{t['calls']}</td><td style='padding:8px 12px;border-top:1px solid #E6E4DD;text-align:right;'>{_fmt_hms(t['talk_time_sec'])}</td></tr>"
    if not rows_html:
        rows_html = "<tr><td colspan='3' style='padding:12px;color:#5C6661;text-align:center;'>No call activity today.</td></tr>"
    return f"""<div style="font-family:Georgia,serif;max-width:640px;margin:0 auto;background:#F6F1E8;padding:32px;color:#102A20;">
<div style="letter-spacing:0.22em;font-size:11px;text-transform:uppercase;color:#5C6661;">Tasko · Daily Summary</div>
<h1 style="font-size:28px;margin:8px 0 4px;letter-spacing:-0.02em;">End of day report</h1>
<div style="color:#5C6661;font-size:14px;">{summary['date']}</div>

<table style="width:100%;margin-top:24px;border-collapse:collapse;background:#fff;border:1px solid #E6E4DD;">
  <tr>
    <td style="padding:16px;border-right:1px solid #E6E4DD;width:33%;">
      <div style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#5C6661;">New leads</div>
      <div style="font-size:32px;font-weight:900;margin-top:4px;">{m['new_leads']}</div>
    </td>
    <td style="padding:16px;border-right:1px solid #E6E4DD;width:33%;">
      <div style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#5C6661;">Bookings</div>
      <div style="font-size:32px;font-weight:900;margin-top:4px;">{m['bookings']}</div>
    </td>
    <td style="padding:16px;width:33%;">
      <div style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#5C6661;">Site visits done</div>
      <div style="font-size:32px;font-weight:900;margin-top:4px;">{m['site_visits_completed']}</div>
    </td>
  </tr>
</table>

<table style="width:100%;margin-top:16px;border-collapse:collapse;background:#fff;border:1px solid #E6E4DD;">
  <tr>
    <td style="padding:16px;border-right:1px solid #E6E4DD;width:25%;">
      <div style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#5C6661;">Total calls</div>
      <div style="font-size:24px;font-weight:800;margin-top:4px;">{c['total']}</div>
    </td>
    <td style="padding:16px;border-right:1px solid #E6E4DD;width:25%;">
      <div style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#5C6661;">Connected</div>
      <div style="font-size:24px;font-weight:800;margin-top:4px;color:#2D6A4F;">{c['connected']}</div>
    </td>
    <td style="padding:16px;border-right:1px solid #E6E4DD;width:25%;">
      <div style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#5C6661;">Missed</div>
      <div style="font-size:24px;font-weight:800;margin-top:4px;color:#C25934;">{c['missed']}</div>
    </td>
    <td style="padding:16px;width:25%;">
      <div style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#5C6661;">Talk time</div>
      <div style="font-size:24px;font-weight:800;margin-top:4px;">{_fmt_hms(c['talk_time_sec'])}</div>
    </td>
  </tr>
</table>

<div style="margin-top:16px;padding:16px;background:#fff;border:1px solid #E6E4DD;">
  <div style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#5C6661;">Follow-ups</div>
  <div style="font-size:14px;margin-top:6px;">Due today: <strong>{f['due_today']}</strong> · Overdue: <strong style="color:#C25934;">{f['overdue']}</strong></div>
</div>

<div style="margin-top:16px;background:#fff;border:1px solid #E6E4DD;">
  <div style="padding:12px 16px;border-bottom:1px solid #E6E4DD;font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#5C6661;">Top executives · today</div>
  <table style="width:100%;border-collapse:collapse;font-size:13px;">
    <thead><tr style="color:#5C6661;text-transform:uppercase;font-size:10px;letter-spacing:0.15em;">
      <th style="padding:8px 12px;text-align:left;">Name</th>
      <th style="padding:8px 12px;text-align:right;">Calls</th>
      <th style="padding:8px 12px;text-align:right;">Talk time</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>

<div style="margin-top:24px;font-size:11px;color:#5C6661;">
  Generated by Tasko CRM · {summary['generated_at']}
</div>
</div>"""


async def send_eod_email_to_admins() -> dict:
    """Send today's summary to all admin users. Uses Resend when available."""
    summary = await compute_eod_summary()
    admins = await db.users.find({"role": "admin", "active": {"$ne": False}}, {"_id": 0}).to_list(50)
    if not admins:
        return {"sent": 0, "reason": "no admins"}
    html = _eod_html(summary)
    subject = f"Tasko · End of day report · {summary['date']}"
    sent = 0
    errors = []
    for a in admins:
        if not a.get("email"):
            continue
        params = {"from": SENDER_EMAIL, "to": [a["email"]], "subject": subject, "html": html}
        try:
            if resend and resend.api_key:
                r = await asyncio.to_thread(resend.Emails.send, params)
                sent += 1
                await db.eod_emails.insert_one({
                    "id": new_id(),
                    "admin_id": a["id"],
                    "email": a["email"],
                    "date": summary["date"],
                    "email_id": r.get("id") if isinstance(r, dict) else None,
                    "created_at": now_utc().isoformat(),
                })
            else:
                log.info("[MOCK] EOD email to %s · subject=%s", a["email"], subject)
                sent += 1
                await db.eod_emails.insert_one({
                    "id": new_id(),
                    "admin_id": a["id"],
                    "email": a["email"],
                    "date": summary["date"],
                    "mock": True,
                    "created_at": now_utc().isoformat(),
                })
        except Exception as exc:
            log.warning("EOD email failed for %s: %s", a["email"], exc)
            errors.append({"email": a["email"], "error": str(exc)})
    return {"sent": sent, "errors": errors, "date": summary["date"]}


@api.get("/admin/eod-summary")
async def admin_eod_summary(user: dict = Depends(require_roles("admin"))):
    return await compute_eod_summary()


@api.post("/admin/eod-email/send")
async def admin_eod_email_send(user: dict = Depends(require_roles("admin"))):
    return await send_eod_email_to_admins()


# ---------------------------------------------------------------------------
# Mount router & CORS
# ---------------------------------------------------------------------------
app.include_router(api)


@app.get("/api/")
async def root():
    return {"service": "Tasko CRM", "version": "1.0.0"}


app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Startup: indexes + admin seeding + demo data
# ---------------------------------------------------------------------------
DEMO_PROJECTS = [
    {
        "name": "Aurelia Heights",
        "location": "Whitefield, Bengaluru",
        "city": "Bengaluru",
        "rera": "PRM/KA/RERA/1251/446/PR/220621/004512",
        "price_min": 12500000,
        "price_max": 32000000,
        "configurations": ["2BHK", "3BHK", "4BHK"],
        "cover": "https://images.pexels.com/photos/15577446/pexels-photo-15577446.jpeg",
        "description": "A premium residential enclave with skyline views and 40+ amenities.",
        "status": "active",
    },
    {
        "name": "Meridian Bay",
        "location": "Bandra West, Mumbai",
        "city": "Mumbai",
        "rera": "P51800009876",
        "price_min": 45000000,
        "price_max": 120000000,
        "configurations": ["3BHK", "4BHK", "Penthouse"],
        "cover": "https://images.pexels.com/photos/5323853/pexels-photo-5323853.jpeg",
        "description": "Sea-facing luxury towers in the heart of Bandra.",
        "status": "active",
    },
    {
        "name": "Cedar Grove",
        "location": "Sector 65, Gurugram",
        "city": "Gurugram",
        "rera": "RC/REP/HARERA/GGM/447/179/2023/45",
        "price_min": 18000000,
        "price_max": 48000000,
        "configurations": ["3BHK", "4BHK"],
        "cover": "https://images.unsplash.com/photo-1614595737476-42487331b8a1",
        "description": "Low-density gated community with private gardens.",
        "status": "active",
    },
]


async def seed_demo():
    # projects
    existing = await db.projects.count_documents({})
    if existing == 0:
        for p in DEMO_PROJECTS:
            doc = {**p, "id": new_id(), "created_at": now_utc().isoformat()}
            await db.projects.insert_one(doc)

    # units - one per project if none
    if await db.units.count_documents({}) == 0:
        projects = await db.projects.find({}, {"_id": 0}).to_list(10)
        rnd = random.Random(42)
        for p in projects:
            configs = p.get("configurations") or ["2BHK", "3BHK"]
            for tower in ["A", "B"]:
                for floor in range(1, 9):
                    for unit_idx in range(1, 5):
                        cfg = configs[unit_idx % len(configs)]
                        price_min = p.get("price_min") or 10000000
                        price_max = p.get("price_max") or 30000000
                        price = int(price_min + (price_max - price_min) * rnd.random())
                        status_r = rnd.random()
                        if status_r < 0.55:
                            unit_status = "available"
                        elif status_r < 0.75:
                            unit_status = "held"
                        elif status_r < 0.92:
                            unit_status = "booked"
                        else:
                            unit_status = "sold"
                        u = {
                            "id": new_id(),
                            "project_id": p["id"],
                            "tower": tower,
                            "floor": floor,
                            "unit_no": f"{tower}-{floor:02d}0{unit_idx}",
                            "config": cfg,
                            "carpet_area": 900 + rnd.randint(0, 900),
                            "price": price,
                            "facing": rnd.choice(["East", "West", "North", "South", "NE", "SW"]),
                            "status": unit_status,
                            "created_at": now_utc().isoformat(),
                        }
                        await db.units.insert_one(u)

    # users
    if await db.users.count_documents({}) == 0:
        seeds = [
            {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "name": "Aditi Kapoor", "role": "admin"},
            {"email": "manager@tasko.com", "password": "manager123", "name": "Rohan Mehra", "role": "manager"},
            {"email": "priya@tasko.com", "password": "executive123", "name": "Priya Sharma", "role": "executive"},
            {"email": "karan@tasko.com", "password": "executive123", "name": "Karan Malhotra", "role": "executive"},
            {"email": "neha@tasko.com", "password": "executive123", "name": "Neha Iyer", "role": "executive"},
        ]
        for s in seeds:
            await db.users.insert_one({
                "id": new_id(),
                "email": s["email"].lower(),
                "name": s["name"],
                "role": s["role"],
                "phone": "",
                "active": True,
                "password_hash": hash_password(s["password"]),
                "created_at": now_utc().isoformat(),
            })
    else:
        # ensure admin password matches env
        admin = await db.users.find_one({"email": ADMIN_EMAIL.lower()})
        if admin and not verify_password(ADMIN_PASSWORD, admin.get("password_hash", "")):
            await db.users.update_one({"email": ADMIN_EMAIL.lower()}, {"$set": {"password_hash": hash_password(ADMIN_PASSWORD)}})

    # leads
    if await db.leads.count_documents({}) == 0:
        projects = await db.projects.find({}, {"_id": 0}).to_list(10)
        execs = await db.users.find({"role": "executive"}, {"_id": 0}).to_list(10)
        first_names = ["Aarav", "Ishaan", "Vihaan", "Ananya", "Diya", "Kabir", "Aditya", "Meera", "Zoya", "Rahul", "Sneha", "Aryan", "Nikhil", "Riya", "Sanya", "Pooja", "Vikas", "Aman", "Simran", "Kunal"]
        last_names = ["Sharma", "Verma", "Gupta", "Kapoor", "Malhotra", "Sinha", "Iyer", "Rao", "Bose", "Chatterjee", "Reddy", "Nair"]
        sources = ["magicbricks", "99acres", "commonfloor", "housing", "website", "google_ads", "facebook", "instagram", "referral", "walk_in"]
        stages = ["new", "contacted", "qualified", "site_visit", "negotiation", "booked", "lost"]
        stage_weights = [0.25, 0.20, 0.15, 0.15, 0.10, 0.08, 0.07]
        priorities = ["hot", "warm", "cold"]
        configs = ["2BHK", "3BHK", "4BHK"]
        rnd = random.Random(7)

        for i in range(48):
            fn = rnd.choice(first_names)
            ln = rnd.choice(last_names)
            proj = rnd.choice(projects) if projects else None
            stage = rnd.choices(stages, weights=stage_weights, k=1)[0]
            assignee = rnd.choice(execs) if execs else None
            created = now_utc() - timedelta(days=rnd.randint(0, 20), hours=rnd.randint(0, 23))
            budget_min = rnd.choice([5000000, 10000000, 15000000, 20000000, 30000000])
            lead = {
                "id": new_id(),
                "name": f"{fn} {ln}",
                "phone": f"+91 9{rnd.randint(100000000, 999999999)}",
                "email": f"{fn.lower()}.{ln.lower()}@example.com",
                "source": rnd.choice(sources),
                "project_id": proj["id"] if proj else None,
                "budget_min": budget_min,
                "budget_max": budget_min + rnd.randint(1000000, 8000000),
                "configuration": rnd.choice(configs),
                "location_pref": (proj or {}).get("city", ""),
                "notes": "",
                "stage": stage,
                "priority": rnd.choice(priorities),
                "stars": rnd.choice([0, 0, 0, 3, 4, 5]),
                "assigned_to": assignee["id"] if assignee else None,
                "created_at": created.isoformat(),
                "updated_at": created.isoformat(),
            }
            await db.leads.insert_one(lead)

    # site visits & follow ups (a few)
    if await db.site_visits.count_documents({}) == 0:
        leads = await db.leads.find({"stage": {"$in": ["site_visit", "negotiation"]}}, {"_id": 0}).to_list(20)
        rnd = random.Random(11)
        for lead in leads[:12]:
            scheduled = now_utc() + timedelta(days=rnd.randint(-2, 7), hours=rnd.randint(9, 18))
            await db.site_visits.insert_one({
                "id": new_id(),
                "lead_id": lead["id"],
                "project_id": lead.get("project_id"),
                "scheduled_at": scheduled.isoformat(),
                "assigned_to": lead.get("assigned_to"),
                "status": "scheduled" if scheduled > now_utc() else rnd.choice(["completed", "no_show"]),
                "notes": "",
                "created_at": now_utc().isoformat(),
            })

    if await db.follow_ups.count_documents({}) == 0:
        leads = await db.leads.find({"stage": {"$nin": ["booked", "lost"]}}, {"_id": 0}).to_list(30)
        rnd = random.Random(13)
        for lead in leads[:20]:
            due = now_utc() + timedelta(hours=rnd.randint(-24, 96))
            await db.follow_ups.insert_one({
                "id": new_id(),
                "lead_id": lead["id"],
                "due_at": due.isoformat(),
                "kind": rnd.choice(["call", "whatsapp", "email"]),
                "notes": "",
                "assigned_to": lead.get("assigned_to"),
                "status": "pending",
                "created_at": now_utc().isoformat(),
            })

    # whatsapp templates
    if await db.whatsapp_templates.count_documents({}) == 0:
        seeds = [
            {"name": "Welcome", "category": "greeting", "body": "Hi {{name}}, thanks for your interest in {{project}}. Our team will connect with you shortly.", "variables": ["name", "project"], "approved": True},
            {"name": "Site Visit Confirmation", "category": "site_visit", "body": "Hi {{name}}, your site visit to {{project}} is confirmed for {{date}} at {{time}}. Reply YES to confirm.", "variables": ["name", "project", "date", "time"], "approved": True},
            {"name": "Follow-up Nudge", "category": "followup", "body": "Hi {{name}}, just checking in on your interest in {{project}}. Any questions I can answer?", "variables": ["name", "project"], "approved": True},
            {"name": "Proposal Sent", "category": "proposal", "body": "Hi {{name}}, we've sent a proposal for {{unit}} at {{project}}. Total: ₹{{amount}}. Valid until {{validity}}.", "variables": ["name", "unit", "project", "amount", "validity"], "approved": False},
        ]
        for s in seeds:
            await db.whatsapp_templates.insert_one({**s, "id": new_id(), "created_at": now_utc().isoformat()})

    # channel partners
    if await db.channel_partners.count_documents({}) == 0:
        seeds = [
            {"name": "Amit Verma", "company": "Verma Realty", "phone": "+91 9800011122", "email": "amit@vermarealty.in", "city": "Bengaluru", "rera": "PRM/KA/2298", "commission_pct": 2.0, "active": True},
            {"name": "Rekha Nair", "company": "SkyLine Advisors", "phone": "+91 9800011133", "email": "rekha@skyline.in", "city": "Mumbai", "rera": "P51900011232", "commission_pct": 1.75, "active": True},
            {"name": "Sameer Khan", "company": "Khan Properties", "phone": "+91 9800011155", "email": "sameer@khanprop.in", "city": "Gurugram", "rera": "RC/REP/HARERA/778", "commission_pct": 2.25, "active": True},
        ]
        for s in seeds:
            await db.channel_partners.insert_one({**s, "id": new_id(), "created_at": now_utc().isoformat(), "notes": ""})

    # proposals (accepted → month revenue)
    if await db.proposals.count_documents({}) == 0:
        booked = await db.leads.find({"stage": "booked"}, {"_id": 0}).to_list(20)
        rnd = random.Random(23)
        for b in booked:
            await db.proposals.insert_one({
                "id": new_id(),
                "lead_id": b["id"],
                "project_id": b.get("project_id"),
                "unit_id": None,
                "amount": rnd.choice([12000000, 18000000, 25000000, 32000000, 48000000]),
                "validity_days": 15,
                "status": "accepted",
                "terms": "Standard terms apply.",
                "created_by": b.get("assigned_to"),
                "created_at": now_utc().isoformat(),
            })
        # a few in draft/sent for pipeline
        neg = await db.leads.find({"stage": "negotiation"}, {"_id": 0}).to_list(10)
        for n in neg[:4]:
            await db.proposals.insert_one({
                "id": new_id(),
                "lead_id": n["id"],
                "project_id": n.get("project_id"),
                "unit_id": None,
                "amount": rnd.choice([15000000, 22000000, 30000000]),
                "validity_days": 15,
                "status": rnd.choice(["sent", "draft"]),
                "terms": "",
                "created_by": n.get("assigned_to"),
                "created_at": now_utc().isoformat(),
            })

    # activities — synthesize outgoing calls / emails / sms across last 4 months so telemetry chart is meaningful
    if await db.activities.count_documents({"kind": {"$in": ["outgoing_call", "email_sent", "sms_sent"]}}) == 0:
        all_leads = await db.leads.find({}, {"id": 1, "assigned_to": 1, "_id": 0}).to_list(200)
        if all_leads:
            rnd = random.Random(29)
            now = now_utc()
            for i in range(220):
                lead = rnd.choice(all_leads)
                kind = rnd.choices(["outgoing_call", "email_sent", "sms_sent", "missed_call"], weights=[0.55, 0.20, 0.20, 0.05])[0]
                days_back = rnd.randint(0, 110)
                ts = now - timedelta(days=days_back, hours=rnd.randint(0, 23))
                meta = {}
                if kind == "outgoing_call":
                    meta = {"direction": "outgoing", "duration_sec": rnd.randint(20, 480), "disposition": "connected", "recording_url": f"https://cdn.tasko.demo/rec/{new_id()}.mp3"}
                elif kind == "missed_call":
                    meta = {"direction": "outgoing", "duration_sec": 0, "disposition": "no_answer"}
                await db.activities.insert_one({
                    "id": new_id(),
                    "lead_id": lead["id"],
                    "actor_id": lead.get("assigned_to"),
                    "actor_name": "System",
                    "kind": kind,
                    "message": kind.replace("_", " ").title(),
                    "meta": meta,
                    "created_at": ts.isoformat(),
                })


from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402

_scheduler: Optional[AsyncIOScheduler] = None


@app.on_event("startup")
async def on_startup():
    global _scheduler
    try:
        await db.users.create_index("email", unique=True)
        await db.leads.create_index("stage")
        await db.leads.create_index("assigned_to")
        await db.leads.create_index("project_id")
        await db.units.create_index([("project_id", 1), ("tower", 1), ("floor", 1)])
        await db.site_visits.create_index("scheduled_at")
        await db.follow_ups.create_index("due_at")
        await db.activities.create_index("lead_id")
        await db.activities.create_index("kind")
        await db.activities.create_index("created_at")
        await db.proposals.create_index("lead_id")
        await db.whatsapp_templates.create_index("name")
        await db.notifications.create_index([("user_id", 1), ("created_at", -1)])
        await db.notifications.create_index([("role_scope", 1), ("created_at", -1)])
        await db.notifications.create_index("dedupe_key")
    except Exception as e:
        log.warning(f"index setup: {e}")
    await seed_demo()

    # Scheduler: EOD email at IST 18:00; notif refresh every 15 minutes
    try:
        eod_hour = int(os.environ.get("EOD_HOUR_IST", "18"))
        eod_min = int(os.environ.get("EOD_MIN_IST", "0"))
        _scheduler = AsyncIOScheduler(timezone=IST)
        _scheduler.add_job(send_eod_email_to_admins, CronTrigger(hour=eod_hour, minute=eod_min), id="eod-email", replace_existing=True)
        _scheduler.add_job(refresh_notifications, "interval", minutes=15, id="notif-refresh", replace_existing=True, next_run_time=now_utc() + timedelta(seconds=30))
        _scheduler.start()
        log.info("Scheduler started (EOD %02d:%02d IST, notif-refresh every 15m)", eod_hour, eod_min)
    except Exception as e:
        log.warning("Scheduler setup failed: %s", e)

    log.info("Tasko CRM startup complete.")


@app.on_event("shutdown")
async def on_shutdown():
    global _scheduler
    try:
        if _scheduler:
            _scheduler.shutdown(wait=False)
    except Exception:
        pass
    client.close()
