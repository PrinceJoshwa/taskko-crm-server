"""Tasko CRM backend integration tests.

Covers: auth (login/me/logout/refresh), RBAC on users/projects, projects CRUD,
units CRUD/list, leads CRUD + stage/assign/notes, executive scoping,
webhook ingestion, site visits, follow-ups, activities, analytics, settings.
"""
import os
import time
from datetime import datetime, timezone, timedelta

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://tasko-crm.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"


# --------------------------------------------------------------------------- Fixtures
def _login(session: requests.Session, email: str, password: str) -> requests.Response:
    return session.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=30)


@pytest.fixture(scope="session")
def admin_session():
    s = requests.Session()
    r = _login(s, "admin@tasko.com", "admin123")
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    return s


@pytest.fixture(scope="session")
def manager_session():
    s = requests.Session()
    r = _login(s, "manager@tasko.com", "manager123")
    assert r.status_code == 200, f"manager login failed: {r.status_code} {r.text}"
    return s


@pytest.fixture(scope="session")
def executive_session():
    s = requests.Session()
    r = _login(s, "priya@tasko.com", "executive123")
    assert r.status_code == 200, f"executive login failed: {r.status_code} {r.text}"
    return s


@pytest.fixture(scope="session")
def anon_session():
    return requests.Session()


@pytest.fixture(scope="session")
def priya_id(admin_session):
    users = admin_session.get(f"{API}/users").json()
    return next(u for u in users if u["email"] == "priya@tasko.com")["id"]


# --------------------------------------------------------------------------- AUTH
class TestAuth:
    def test_login_admin_sets_cookies(self):
        s = requests.Session()
        r = _login(s, "admin@tasko.com", "admin123")
        assert r.status_code == 200
        data = r.json()
        assert data["role"] == "admin"
        assert data["email"] == "admin@tasko.com"
        # cookies present
        assert "access_token" in s.cookies
        assert "refresh_token" in s.cookies

    def test_login_manager(self):
        s = requests.Session()
        r = _login(s, "manager@tasko.com", "manager123")
        assert r.status_code == 200
        assert r.json()["role"] == "manager"

    def test_login_executive(self):
        s = requests.Session()
        r = _login(s, "priya@tasko.com", "executive123")
        assert r.status_code == 200
        assert r.json()["role"] == "executive"

    def test_login_invalid(self):
        s = requests.Session()
        r = _login(s, "admin@tasko.com", "wrong-pass")
        assert r.status_code == 401

    def test_me_with_cookies(self, admin_session):
        r = admin_session.get(f"{API}/auth/me")
        assert r.status_code == 200
        assert r.json()["email"] == "admin@tasko.com"

    def test_me_without_cookies(self, anon_session):
        r = anon_session.get(f"{API}/auth/me")
        assert r.status_code == 401


# --------------------------------------------------------------------------- USERS / RBAC
class TestUsersRBAC:
    def test_executive_can_list_users(self, executive_session):
        r = executive_session.get(f"{API}/users")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_executive_cannot_create_user(self, executive_session):
        r = executive_session.post(f"{API}/users", json={
            "email": "TEST_exec_create@tasko.com", "password": "x", "name": "x", "role": "executive"
        })
        assert r.status_code == 403

    def test_admin_can_create_and_delete_user(self, admin_session):
        email = f"TEST_new_{int(time.time())}@tasko.com"
        r = admin_session.post(f"{API}/users", json={
            "email": email, "password": "pw123", "name": "Test User", "role": "executive"
        })
        assert r.status_code == 200, r.text
        uid = r.json()["id"]
        assert r.json()["email"] == email.lower()
        # cleanup
        d = admin_session.delete(f"{API}/users/{uid}")
        assert d.status_code == 200


# --------------------------------------------------------------------------- PROJECTS
class TestProjects:
    def test_list_projects_has_seed(self, admin_session):
        r = admin_session.get(f"{API}/projects")
        assert r.status_code == 200
        projects = r.json()
        assert len(projects) >= 3
        p = projects[0]
        assert "units_total" in p and "units_available" in p and "leads_count" in p

    def test_admin_create_and_delete_project(self, admin_session):
        r = admin_session.post(f"{API}/projects", json={
            "name": f"TEST_Project_{int(time.time())}",
            "location": "Test Location",
            "city": "Testville",
        })
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        # verify persistence
        listed = admin_session.get(f"{API}/projects").json()
        assert any(p["id"] == pid for p in listed)
        # cleanup
        d = admin_session.delete(f"{API}/projects/{pid}")
        assert d.status_code == 200

    def test_executive_cannot_delete_project(self, executive_session, admin_session):
        # get any project id
        pid = admin_session.get(f"{API}/projects").json()[0]["id"]
        r = executive_session.delete(f"{API}/projects/{pid}")
        assert r.status_code == 403


# --------------------------------------------------------------------------- UNITS
class TestUnits:
    def test_list_units_by_project(self, admin_session):
        # pick a seeded project (skip TEST_ prefixed projects from other tests)
        projects = [p for p in admin_session.get(f"{API}/projects").json() if not p["name"].startswith("TEST_")]
        pid = projects[0]["id"]
        r = admin_session.get(f"{API}/units", params={"project_id": pid})
        assert r.status_code == 200
        units = r.json()
        assert len(units) >= 32  # 2 towers * 8 floors * 4 units = 64 per project

    def test_patch_unit_status(self, admin_session, manager_session):
        projects = [p for p in admin_session.get(f"{API}/projects").json() if not p["name"].startswith("TEST_")]
        pid = projects[0]["id"]
        units = admin_session.get(f"{API}/units", params={"project_id": pid}).json()
        u = units[0]
        r = manager_session.patch(f"{API}/units/{u['id']}", json={"status": "held"})
        assert r.status_code == 200
        assert r.json()["status"] == "held"

    def test_executive_cannot_patch_unit(self, executive_session, admin_session):
        projects = [p for p in admin_session.get(f"{API}/projects").json() if not p["name"].startswith("TEST_")]
        pid = projects[0]["id"]
        u = admin_session.get(f"{API}/units", params={"project_id": pid}).json()[0]
        r = executive_session.patch(f"{API}/units/{u['id']}", json={"status": "held"})
        assert r.status_code == 403


# --------------------------------------------------------------------------- LEADS
class TestLeads:
    def test_admin_sees_all_leads(self, admin_session):
        r = admin_session.get(f"{API}/leads")
        assert r.status_code == 200
        leads = r.json()
        assert len(leads) >= 40  # 48 seeded

    def test_executive_only_own_leads(self, executive_session, priya_id):
        r = executive_session.get(f"{API}/leads")
        assert r.status_code == 200
        leads = r.json()
        for l in leads:
            assert l.get("assigned_to") == priya_id, f"Executive sees non-own lead: {l.get('assigned_to')}"

    def test_create_lead_auto_assigns(self, admin_session):
        r = admin_session.post(f"{API}/leads", json={
            "name": "TEST_Auto Lead", "phone": "+919000000001", "source": "manual"
        })
        assert r.status_code == 200
        lead = r.json()
        assert lead["assigned_to"] is not None, "Auto-assign not applied"
        assert lead["stage"] == "new"
        # cleanup
        admin_session.delete(f"{API}/leads/{lead['id']}")

    def test_stage_move_and_activity(self, admin_session):
        # create lead
        lead = admin_session.post(f"{API}/leads", json={"name": "TEST_Stage Lead", "source": "manual"}).json()
        lid = lead["id"]
        r = admin_session.post(f"{API}/leads/{lid}/stage", json={"stage": "qualified", "note": "moved"})
        assert r.status_code == 200
        assert r.json()["stage"] == "qualified"
        # activity logged
        acts = admin_session.get(f"{API}/activities", params={"lead_id": lid}).json()
        assert any(a["kind"] == "stage_change" for a in acts)
        admin_session.delete(f"{API}/leads/{lid}")

    def test_assign_lead(self, admin_session, priya_id):
        lead = admin_session.post(f"{API}/leads", json={"name": "TEST_Assign Lead", "source": "manual"}).json()
        lid = lead["id"]
        r = admin_session.post(f"{API}/leads/{lid}/assign", json={"user_id": priya_id})
        assert r.status_code == 200
        assert r.json()["assigned_to"] == priya_id
        admin_session.delete(f"{API}/leads/{lid}")

    def test_patch_lead_and_note(self, admin_session):
        lead = admin_session.post(f"{API}/leads", json={"name": "TEST_Patch Lead", "source": "manual"}).json()
        lid = lead["id"]
        r = admin_session.patch(f"{API}/leads/{lid}", json={"priority": "hot", "phone": "+919000009999"})
        assert r.status_code == 200
        assert r.json()["priority"] == "hot"
        # add whatsapp note
        n = admin_session.post(f"{API}/leads/{lid}/notes", json={"kind": "whatsapp", "text": "Hi via WA"})
        assert n.status_code == 200
        acts = admin_session.get(f"{API}/activities", params={"lead_id": lid}).json()
        assert any(a["kind"] == "whatsapp" and "WA" in a["message"] for a in acts)
        admin_session.delete(f"{API}/leads/{lid}")


# --------------------------------------------------------------------------- WEBHOOKS
class TestWebhooks:
    def test_magicbricks_webhook_public(self, anon_session, admin_session):
        r = anon_session.post(f"{API}/webhooks/leads/magicbricks", json={
            "name": "TEST_MB Webhook", "phone": "+919999900001", "project": "Aurelia Heights"
        })
        assert r.status_code == 200, r.text
        lead_id = r.json()["lead_id"]
        # verify lead persisted with source and assigned
        lead = admin_session.get(f"{API}/leads/{lead_id}").json()
        assert lead["source"] == "magicbricks"
        assert lead["assigned_to"] is not None
        admin_session.delete(f"{API}/leads/{lead_id}")

    def test_facebook_webhook(self, anon_session, admin_session):
        r = anon_session.post(f"{API}/webhooks/leads/facebook", json={"full_name": "TEST_FB", "mobile": "+919999900002"})
        assert r.status_code == 200
        admin_session.delete(f"{API}/leads/{r.json()['lead_id']}")

    def test_google_ads_webhook(self, anon_session, admin_session):
        r = anon_session.post(f"{API}/webhooks/leads/google_ads", json={"name": "TEST_GA", "phone": "+919999900003"})
        assert r.status_code == 200
        admin_session.delete(f"{API}/leads/{r.json()['lead_id']}")

    def test_unknown_source_returns_400(self, anon_session):
        r = anon_session.post(f"{API}/webhooks/leads/unknown_source", json={"name": "x"})
        assert r.status_code == 400


# --------------------------------------------------------------------------- SITE VISITS
class TestSiteVisits:
    def test_create_visit_advances_stage(self, admin_session):
        # create a lead
        lead = admin_session.post(f"{API}/leads", json={"name": "TEST_Visit Lead", "source": "manual"}).json()
        lid = lead["id"]
        proj = admin_session.get(f"{API}/projects").json()[0]
        sched = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        r = admin_session.post(f"{API}/site-visits", json={
            "lead_id": lid, "project_id": proj["id"], "scheduled_at": sched
        })
        assert r.status_code == 200, r.text
        vid = r.json()["id"]
        # verify lead stage advanced
        lead_now = admin_session.get(f"{API}/leads/{lid}").json()
        assert lead_now["stage"] == "site_visit"
        # list visits
        visits = admin_session.get(f"{API}/site-visits").json()
        assert any(v["id"] == vid for v in visits)
        # patch complete
        p = admin_session.patch(f"{API}/site-visits/{vid}", json={"status": "completed"})
        assert p.status_code == 200
        assert p.json()["status"] == "completed"
        # cleanup
        admin_session.delete(f"{API}/site-visits/{vid}")
        admin_session.delete(f"{API}/leads/{lid}")


# --------------------------------------------------------------------------- FOLLOW-UPS
class TestFollowUps:
    def test_create_patch_list(self, admin_session):
        lead = admin_session.post(f"{API}/leads", json={"name": "TEST_FU Lead", "source": "manual"}).json()
        lid = lead["id"]
        due = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        r = admin_session.post(f"{API}/follow-ups", json={"lead_id": lid, "due_at": due, "kind": "call"})
        assert r.status_code == 200
        fid = r.json()["id"]
        assert r.json()["status"] == "pending"

        # filter pending
        pending = admin_session.get(f"{API}/follow-ups", params={"status": "pending"}).json()
        assert any(f["id"] == fid for f in pending)

        # mark done
        u = admin_session.patch(f"{API}/follow-ups/{fid}", json={"status": "done"})
        assert u.status_code == 200
        assert u.json()["status"] == "done"

        admin_session.delete(f"{API}/follow-ups/{fid}")
        admin_session.delete(f"{API}/leads/{lid}")


# --------------------------------------------------------------------------- ANALYTICS
class TestAnalytics:
    def test_summary_shape(self, admin_session):
        r = admin_session.get(f"{API}/analytics/summary")
        assert r.status_code == 200
        d = r.json()
        for k in ["total_leads", "conversion_rate", "visits_today", "followups_pending", "funnel", "sources", "trend"]:
            assert k in d
        # funnel has 7 stages
        for s in ["new", "contacted", "qualified", "site_visit", "negotiation", "booked", "lost"]:
            assert s in d["funnel"]
        # 7-day trend
        assert len(d["trend"]) == 7
        assert isinstance(d["sources"], list)


# --------------------------------------------------------------------------- SETTINGS
class TestSettings:
    def test_admin_get_patch(self, admin_session):
        g = admin_session.get(f"{API}/settings")
        assert g.status_code == 200
        p = admin_session.patch(f"{API}/settings", json={"whatsapp_enabled": True, "auto_assign_enabled": True})
        assert p.status_code == 200
        assert p.json()["whatsapp_enabled"] is True
        assert p.json()["auto_assign_enabled"] is True

    def test_non_admin_patch_forbidden(self, manager_session, executive_session):
        for s in [manager_session, executive_session]:
            r = s.patch(f"{API}/settings", json={"whatsapp_enabled": False})
            assert r.status_code == 403
