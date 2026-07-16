"""Tasko CRM iteration 2 backend tests: Dashboard, My Console modules.

Covers: log-call/sms/email, bulk-assign, import, whatsapp-templates,
channel-partners, proposals, dashboard/monthly, dashboard/action-items,
reports/executives, reports/sources, star rating update.
"""
import os
import time
from datetime import datetime, timezone, timedelta

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://tasko-crm.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"


def _login(session, email, password):
    return session.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=30)


# ------ Fixtures
@pytest.fixture(scope="session")
def admin_session():
    s = requests.Session()
    r = _login(s, "admin@tasko.com", "admin123")
    assert r.status_code == 200
    return s


@pytest.fixture(scope="session")
def manager_session():
    s = requests.Session()
    r = _login(s, "manager@tasko.com", "manager123")
    assert r.status_code == 200
    return s


@pytest.fixture(scope="session")
def executive_session():
    s = requests.Session()
    r = _login(s, "priya@tasko.com", "executive123")
    assert r.status_code == 200
    return s


@pytest.fixture(scope="session")
def priya_id(admin_session):
    users = admin_session.get(f"{API}/users").json()
    return next(u for u in users if u["email"] == "priya@tasko.com")["id"]


@pytest.fixture()
def temp_lead(admin_session):
    r = admin_session.post(f"{API}/leads", json={"name": "TEST_L2 " + str(time.time()), "source": "manual"})
    assert r.status_code == 200, r.text
    lid = r.json()["id"]
    yield lid
    admin_session.delete(f"{API}/leads/{lid}")


# ------ Star rating patch
class TestStarRating:
    def test_patch_stars(self, admin_session, temp_lead):
        r = admin_session.patch(f"{API}/leads/{temp_lead}", json={"stars": 5})
        assert r.status_code == 200, r.text
        assert r.json()["stars"] == 5
        # verify persistence
        g = admin_session.get(f"{API}/leads/{temp_lead}").json()
        assert g["stars"] == 5


# ------ Log call / sms / email
class TestLogEndpoints:
    def test_log_call_outgoing_connected(self, admin_session, temp_lead):
        r = admin_session.post(f"{API}/leads/{temp_lead}/log-call", json={
            "direction": "outgoing", "duration_sec": 120, "disposition": "connected",
            "recording_url": "https://demo/rec.mp3", "notes": "hi"
        })
        assert r.status_code == 200
        acts = admin_session.get(f"{API}/activities", params={"lead_id": temp_lead}).json()
        call_acts = [a for a in acts if a["kind"] == "outgoing_call"]
        assert len(call_acts) >= 1
        assert call_acts[0]["meta"].get("recording_url") == "https://demo/rec.mp3"

    def test_log_call_no_answer_creates_missed(self, admin_session, temp_lead):
        r = admin_session.post(f"{API}/leads/{temp_lead}/log-call", json={
            "direction": "outgoing", "duration_sec": 0, "disposition": "no_answer"
        })
        assert r.status_code == 200
        acts = admin_session.get(f"{API}/activities", params={"lead_id": temp_lead}).json()
        assert any(a["kind"] == "missed_call" for a in acts)
        # must NOT be outgoing_call
        for a in acts:
            if a.get("meta", {}).get("disposition") == "no_answer":
                assert a["kind"] == "missed_call"

    def test_log_sms(self, admin_session, temp_lead):
        r = admin_session.post(f"{API}/leads/{temp_lead}/log-sms", json={"text": "hi"})
        assert r.status_code == 200
        acts = admin_session.get(f"{API}/activities", params={"lead_id": temp_lead}).json()
        assert any(a["kind"] == "sms_sent" for a in acts)

    def test_log_email(self, admin_session, temp_lead):
        r = admin_session.post(f"{API}/leads/{temp_lead}/log-email", json={"subject": "Hello", "body": "world"})
        assert r.status_code == 200
        acts = admin_session.get(f"{API}/activities", params={"lead_id": temp_lead}).json()
        assert any(a["kind"] == "email_sent" for a in acts)


# ------ Bulk assign
class TestBulkAssign:
    def test_bulk_assign_admin(self, admin_session, priya_id):
        # Create 3 leads
        ids = []
        for i in range(3):
            r = admin_session.post(f"{API}/leads", json={"name": f"TEST_Bulk_{i}_{time.time()}", "source": "manual"})
            ids.append(r.json()["id"])
        try:
            r = admin_session.post(f"{API}/leads/bulk-assign", json={"lead_ids": ids, "user_id": priya_id})
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["matched"] == 3
            assert data["modified"] >= 0  # some may already be assigned
            # Verify each lead's assigned_to
            for lid in ids:
                lead = admin_session.get(f"{API}/leads/{lid}").json()
                assert lead["assigned_to"] == priya_id
        finally:
            for lid in ids:
                admin_session.delete(f"{API}/leads/{lid}")

    def test_bulk_assign_executive_forbidden(self, executive_session, priya_id):
        r = executive_session.post(f"{API}/leads/bulk-assign", json={"lead_ids": ["x"], "user_id": priya_id})
        assert r.status_code == 403


# ------ CSV import
class TestImport:
    def test_import_two_rows(self, admin_session):
        # get seed project
        proj = admin_session.get(f"{API}/projects").json()[0]
        rows = [
            {"name": "TEST_Import_A", "phone": "+919000000101", "source": "website", "project_name": proj["name"]},
            {"name": "TEST_Import_B", "phone": "+919000000102", "source": "google_ads"},
        ]
        r = admin_session.post(f"{API}/leads/import", json={"rows": rows, "auto_assign": True})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["created"] == 2
        assert data["failed"] == 0
        # verify by search
        leads = admin_session.get(f"{API}/leads", params={"search": "TEST_Import_"}).json()
        assert len(leads) >= 2
        a = next(l for l in leads if l["name"] == "TEST_Import_A")
        assert a["source"] == "website"
        assert a["project_id"] == proj["id"]
        # cleanup
        for l in leads:
            if l["name"].startswith("TEST_Import_"):
                admin_session.delete(f"{API}/leads/{l['id']}")


# ------ WhatsApp templates
class TestWhatsAppTemplates:
    def test_list_seeded(self, admin_session):
        r = admin_session.get(f"{API}/whatsapp-templates")
        assert r.status_code == 200
        templates = r.json()
        names = [t["name"] for t in templates]
        for expected in ["Welcome", "Site Visit Confirmation", "Follow-up Nudge", "Proposal Sent"]:
            assert expected in names, f"Missing seed template {expected}"

    def test_create_patch_delete(self, admin_session):
        payload = {"name": f"TEST_WA_{time.time()}", "category": "test", "body": "Hello {{name}}", "variables": ["name"]}
        r = admin_session.post(f"{API}/whatsapp-templates", json=payload)
        assert r.status_code == 200
        tid = r.json()["id"]
        # patch - toggle approved
        p = admin_session.patch(f"{API}/whatsapp-templates/{tid}", json={**payload, "approved": True})
        assert p.status_code == 200
        assert p.json()["approved"] is True
        # delete
        d = admin_session.delete(f"{API}/whatsapp-templates/{tid}")
        assert d.status_code == 200

    def test_executive_cannot_create(self, executive_session):
        r = executive_session.post(f"{API}/whatsapp-templates", json={"name": "TEST_X", "body": "hi"})
        assert r.status_code == 403


# ------ Channel partners
class TestChannelPartners:
    def test_list_seeded_three(self, admin_session):
        r = admin_session.get(f"{API}/channel-partners")
        assert r.status_code == 200
        partners = r.json()
        assert len(partners) >= 3
        for p in partners:
            assert "leads_count" in p

    def test_create_patch_delete_admin_only(self, admin_session, manager_session):
        payload = {"name": f"TEST_CP_{time.time()}", "company": "Test Realty", "commission_pct": 1.5}
        r = admin_session.post(f"{API}/channel-partners", json=payload)
        assert r.status_code == 200
        pid = r.json()["id"]
        # manager can patch
        pt = manager_session.patch(f"{API}/channel-partners/{pid}", json={**payload, "commission_pct": 2.0})
        assert pt.status_code == 200
        assert pt.json()["commission_pct"] == 2.0
        # only admin can delete
        md = manager_session.delete(f"{API}/channel-partners/{pid}")
        assert md.status_code == 403
        d = admin_session.delete(f"{API}/channel-partners/{pid}")
        assert d.status_code == 200


# ------ Proposals
class TestProposals:
    def test_list(self, admin_session):
        r = admin_session.get(f"{API}/proposals")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_create_and_accept_flow(self, admin_session, temp_lead):
        r = admin_session.post(f"{API}/proposals", json={"lead_id": temp_lead, "amount": 5000000, "status": "draft"})
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        assert r.json()["status"] == "draft"
        # patch to accepted
        p = admin_session.patch(f"{API}/proposals/{pid}", json={"status": "accepted"})
        assert p.status_code == 200
        assert p.json()["status"] == "accepted"
        # verify activity logged
        acts = admin_session.get(f"{API}/activities", params={"lead_id": temp_lead}).json()
        assert any(a["kind"] == "proposal_accepted" for a in acts)
        # cleanup
        admin_session.delete(f"{API}/proposals/{pid}")


# ------ Dashboard endpoints
class TestDashboard:
    def test_monthly_shape(self, admin_session):
        r = admin_session.get(f"{API}/dashboard/monthly")
        assert r.status_code == 200
        d = r.json()
        for k in ["kpi", "revenue", "telemetry", "pipeline", "top_leads", "recent_inquiries", "upcoming_closures"]:
            assert k in d, f"missing {k}"
        # kpi
        for k in ["new_leads", "revenue", "booked", "conversion"]:
            assert k in d["kpi"]
        # revenue
        for k in ["current", "previous", "change_pct"]:
            assert k in d["revenue"]
        # telemetry - 4 rows
        assert len(d["telemetry"]) == 4
        for row in d["telemetry"]:
            for k in ["outgoing_call", "email_sent", "sms_sent", "followup_scheduled"]:
                assert k in row
        # pipeline - 7 stages
        stages = {p["stage"] for p in d["pipeline"]}
        assert stages == {"new", "contacted", "qualified", "site_visit", "negotiation", "booked", "lost"}
        # top_leads sorted by stars desc where stars > 0
        starred = [l for l in d["top_leads"] if l.get("stars", 0) > 0]
        if len(starred) >= 2:
            for i in range(len(starred) - 1):
                assert starred[i]["stars"] >= starred[i + 1]["stars"]

    def test_action_items_shape(self, admin_session):
        r = admin_session.get(f"{API}/dashboard/action-items")
        assert r.status_code == 200
        d = r.json()
        for k in ["widgets", "todays_followups", "planned_visits", "no_call_leads", "no_followup_leads"]:
            assert k in d
        for w in ["missed_calls", "todays_followups", "scheduled_calls", "tasks"]:
            assert w in d["widgets"]


# ------ Reports
class TestReports:
    def test_executive_report(self, admin_session):
        r = admin_session.get(f"{API}/reports/executives")
        assert r.status_code == 200
        rows = r.json()
        assert isinstance(rows, list)
        assert len(rows) >= 3  # 3 seeded executives
        for row in rows:
            for k in ["id", "name", "email", "leads", "booked", "site_visits", "pending_followups", "conversion"]:
                assert k in row

    def test_source_report(self, admin_session):
        r = admin_session.get(f"{API}/reports/sources")
        assert r.status_code == 200
        rows = r.json()
        assert isinstance(rows, list)
        for row in rows:
            for k in ["source", "total", "booked", "conversion"]:
                assert k in row
