"""Iteration 4 — Backend tests for:
  - Dashboard revenue-breakdown drilldown
  - Lead delete admin-only
  - Lead edit permissions (executive can edit own, not others)
  - Executive scoping regression on list/PATCH/dashboard
  - Inventory CSV export (auth + content-type + rows)
  - Inventory CSV import (admin/manager only, replace_existing)
  - Regression: /twilio/status + settings + auth-me
"""
import os
import io
import csv
import uuid
import time
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/") if os.environ.get("REACT_APP_BACKEND_URL") else None
# Fallback for backend .env-driven runs
if not BASE_URL:
    BASE_URL = "http://localhost:8001"

API = f"{BASE_URL}/api"

ADMIN = ("admin@tasko.com", "admin123")
MANAGER = ("manager@tasko.com", "manager123")
PRIYA = ("priya@tasko.com", "executive123")
KARAN = ("karan@tasko.com", "executive123")


def _login(email, password):
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=15)
    assert r.status_code == 200, f"login failed for {email}: {r.status_code} {r.text}"
    return s


@pytest.fixture(scope="session")
def admin_s():
    return _login(*ADMIN)


@pytest.fixture(scope="session")
def manager_s():
    return _login(*MANAGER)


@pytest.fixture(scope="session")
def priya_s():
    return _login(*PRIYA)


@pytest.fixture(scope="session")
def karan_s():
    return _login(*KARAN)


@pytest.fixture(scope="session")
def priya_id(admin_s):
    users = admin_s.get(f"{API}/users").json()
    u = next(u for u in users if u["email"] == "priya@tasko.com")
    return u["id"]


@pytest.fixture(scope="session")
def karan_id(admin_s):
    users = admin_s.get(f"{API}/users").json()
    u = next(u for u in users if u["email"] == "karan@tasko.com")
    return u["id"]


@pytest.fixture(scope="session")
def some_project_id(admin_s):
    projs = admin_s.get(f"{API}/projects").json()
    assert len(projs) > 0, "No projects seeded"
    return projs[0]["id"]


# ---------------------------------------------------------------------------
# 1. Dashboard revenue-breakdown
# ---------------------------------------------------------------------------
class TestRevenueBreakdown:
    def test_admin_gets_breakdown_structure(self, admin_s):
        r = admin_s.get(f"{API}/dashboard/revenue-breakdown")
        assert r.status_code == 200
        d = r.json()
        assert set(d.keys()) >= {"total", "by_agent", "by_project", "period"}
        assert isinstance(d["by_agent"], list)
        assert isinstance(d["by_project"], list)
        assert isinstance(d["total"], (int, float))
        # Period is dict with start & end
        assert "start" in d["period"] and "end" in d["period"]

    def test_unauthenticated_401(self):
        r = requests.get(f"{API}/dashboard/revenue-breakdown", timeout=10)
        assert r.status_code == 401

    def test_executive_scoped_breakdown(self, priya_s, admin_s):
        r_exec = priya_s.get(f"{API}/dashboard/revenue-breakdown").json()
        r_admin = admin_s.get(f"{API}/dashboard/revenue-breakdown").json()
        # Executive total <= admin total (since exec sees only her own)
        assert r_exec["total"] <= r_admin["total"]


# ---------------------------------------------------------------------------
# 2. Lead delete admin-only
# ---------------------------------------------------------------------------
class TestLeadDelete:
    def _make_lead(self, session, name="TEST_delete_lead"):
        r = session.post(f"{API}/leads", json={"name": name, "phone": "+919000000001"})
        assert r.status_code == 200, r.text
        return r.json()["id"]

    def test_delete_lead_admin_200(self, admin_s):
        lead_id = self._make_lead(admin_s, "TEST_del_admin")
        r = admin_s.delete(f"{API}/leads/{lead_id}")
        assert r.status_code == 200
        # verify gone
        r2 = admin_s.get(f"{API}/leads/{lead_id}")
        assert r2.status_code == 404

    def test_delete_lead_manager_403(self, admin_s, manager_s):
        lead_id = self._make_lead(admin_s, "TEST_del_mgr")
        try:
            r = manager_s.delete(f"{API}/leads/{lead_id}")
            assert r.status_code == 403
        finally:
            admin_s.delete(f"{API}/leads/{lead_id}")

    def test_delete_lead_executive_403(self, admin_s, priya_s, priya_id):
        # assign lead to priya
        r0 = admin_s.post(f"{API}/leads", json={"name": "TEST_del_exec", "phone": "+919000000002", "assigned_to": priya_id})
        assert r0.status_code == 200
        lead_id = r0.json()["id"]
        try:
            r = priya_s.delete(f"{API}/leads/{lead_id}")
            assert r.status_code == 403
        finally:
            admin_s.delete(f"{API}/leads/{lead_id}")


# ---------------------------------------------------------------------------
# 3. Lead edit permissions
# ---------------------------------------------------------------------------
class TestLeadEditPermissions:
    def test_admin_patch_any_lead(self, admin_s, priya_id):
        r0 = admin_s.post(f"{API}/leads", json={"name": "TEST_edit_admin", "assigned_to": priya_id})
        lid = r0.json()["id"]
        try:
            r = admin_s.patch(f"{API}/leads/{lid}", json={"name": "TEST_edit_admin_2"})
            assert r.status_code == 200
            assert r.json()["name"] == "TEST_edit_admin_2"
        finally:
            admin_s.delete(f"{API}/leads/{lid}")

    def test_manager_patch_any_lead(self, admin_s, manager_s, priya_id):
        r0 = admin_s.post(f"{API}/leads", json={"name": "TEST_edit_mgr", "assigned_to": priya_id})
        lid = r0.json()["id"]
        try:
            r = manager_s.patch(f"{API}/leads/{lid}", json={"notes": "mgr edit"})
            assert r.status_code == 200
            assert r.json()["notes"] == "mgr edit"
        finally:
            admin_s.delete(f"{API}/leads/{lid}")

    def test_executive_patch_own_lead_ok(self, admin_s, priya_s, priya_id):
        r0 = admin_s.post(f"{API}/leads", json={"name": "TEST_edit_own", "assigned_to": priya_id})
        lid = r0.json()["id"]
        try:
            r = priya_s.patch(f"{API}/leads/{lid}", json={"name": "TEST_edit_own_by_priya"})
            assert r.status_code == 200
            assert r.json()["name"] == "TEST_edit_own_by_priya"
        finally:
            admin_s.delete(f"{API}/leads/{lid}")

    def test_executive_patch_others_lead_403(self, admin_s, priya_s, karan_id):
        r0 = admin_s.post(f"{API}/leads", json={"name": "TEST_edit_other", "assigned_to": karan_id})
        lid = r0.json()["id"]
        try:
            r = priya_s.patch(f"{API}/leads/{lid}", json={"name": "cross-edit"})
            assert r.status_code == 403
            body = r.json()
            assert "own leads" in str(body.get("detail", "")).lower() or "only edit" in str(body.get("detail", "")).lower()
        finally:
            admin_s.delete(f"{API}/leads/{lid}")


# ---------------------------------------------------------------------------
# 4. Executive scoping regression
# ---------------------------------------------------------------------------
class TestExecutiveScoping:
    def test_priya_list_leads_only_hers(self, priya_s, priya_id):
        r = priya_s.get(f"{API}/leads")
        assert r.status_code == 200
        for l in r.json():
            assert l.get("assigned_to") == priya_id, f"Lead {l['id']} not assigned to priya"

    def test_priya_dashboard_monthly_scoped(self, priya_s, admin_s):
        r_exec = priya_s.get(f"{API}/dashboard/monthly").json()
        r_admin = admin_s.get(f"{API}/dashboard/monthly").json()
        # exec's new_leads should be <= admin's overall
        assert r_exec["kpi"]["new_leads"] <= r_admin["kpi"]["new_leads"]


# ---------------------------------------------------------------------------
# 5. Inventory CSV Export
# ---------------------------------------------------------------------------
class TestInventoryExport:
    def test_unauthenticated_export_401(self, some_project_id):
        r = requests.get(f"{API}/units/export", params={"project_id": some_project_id}, timeout=15)
        assert r.status_code == 401

    def test_admin_export_returns_csv(self, admin_s, some_project_id):
        r = admin_s.get(f"{API}/units/export", params={"project_id": some_project_id})
        assert r.status_code == 200
        ct = r.headers.get("content-type", "")
        assert "text/csv" in ct, f"content-type was {ct}"
        text = r.text
        first_line = text.split("\n")[0]
        expected = "tower,floor,unit_no,config,carpet_area,price,facing,status"
        assert first_line.strip() == expected, f"header wrong: {first_line!r}"

    def test_executive_export_ok(self, priya_s, some_project_id):
        # spec allows any authenticated user to export
        r = priya_s.get(f"{API}/units/export", params={"project_id": some_project_id})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# 6. Inventory CSV Import
# ---------------------------------------------------------------------------
class TestInventoryImport:
    def test_import_admin_creates_units(self, admin_s, some_project_id):
        tower_tag = f"TESTC{uuid.uuid4().hex[:4].upper()}"
        rows = [
            {"tower": tower_tag, "floor": 1, "unit_no": f"{tower_tag}-0101",
             "config": "3BHK", "carpet_area": 1200, "price": 15000000,
             "facing": "East", "status": "available"},
            {"tower": tower_tag, "floor": 2, "unit_no": f"{tower_tag}-0201",
             "config": "3BHK", "carpet_area": 1250, "price": 16000000,
             "facing": "West", "status": "available"},
        ]
        r = admin_s.post(f"{API}/units/import",
                         json={"project_id": some_project_id, "rows": rows, "replace_existing": False})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["created"] == 2
        assert d["failed"] == 0
        assert d["replaced"] is False

        # Verify persistence
        units = admin_s.get(f"{API}/units", params={"project_id": some_project_id}).json()
        matching = [u for u in units if u["tower"] == tower_tag]
        assert len(matching) == 2
        # cleanup
        for u in matching:
            admin_s.delete(f"{API}/units/{u['id']}")

    def test_import_manager_ok(self, manager_s, some_project_id):
        tower_tag = f"TESTM{uuid.uuid4().hex[:4].upper()}"
        r = manager_s.post(f"{API}/units/import", json={
            "project_id": some_project_id,
            "rows": [{"tower": tower_tag, "floor": 1, "unit_no": f"{tower_tag}-1", "config": "2BHK"}],
        })
        assert r.status_code == 200
        # cleanup via admin
        admin = _login(*ADMIN)
        units = admin.get(f"{API}/units", params={"project_id": some_project_id}).json()
        for u in [x for x in units if x["tower"] == tower_tag]:
            admin.delete(f"{API}/units/{u['id']}")

    def test_import_executive_403(self, priya_s, some_project_id):
        r = priya_s.post(f"{API}/units/import", json={
            "project_id": some_project_id,
            "rows": [{"tower": "X", "floor": 1, "unit_no": "X-1", "config": "2BHK"}],
        })
        assert r.status_code == 403

    def test_import_replace_existing_wipes(self, admin_s):
        # Create a fresh project so replace_existing wipes a known set
        pr = admin_s.post(f"{API}/projects", json={"name": f"TEST_INV_{uuid.uuid4().hex[:6]}", "location": "Test"})
        assert pr.status_code == 200, pr.text
        pid = pr.json()["id"]
        try:
            # seed 3 units
            admin_s.post(f"{API}/units/import", json={
                "project_id": pid,
                "rows": [
                    {"tower": "A", "floor": 1, "unit_no": "A-1", "config": "2BHK"},
                    {"tower": "A", "floor": 1, "unit_no": "A-2", "config": "2BHK"},
                    {"tower": "A", "floor": 1, "unit_no": "A-3", "config": "2BHK"},
                ],
            })
            u1 = admin_s.get(f"{API}/units", params={"project_id": pid}).json()
            assert len(u1) == 3
            # replace with 1 unit
            r = admin_s.post(f"{API}/units/import", json={
                "project_id": pid,
                "rows": [{"tower": "B", "floor": 1, "unit_no": "B-1", "config": "3BHK"}],
                "replace_existing": True,
            })
            assert r.status_code == 200
            assert r.json()["replaced"] is True
            u2 = admin_s.get(f"{API}/units", params={"project_id": pid}).json()
            assert len(u2) == 1
            assert u2[0]["tower"] == "B"
        finally:
            admin_s.delete(f"{API}/projects/{pid}")

    def test_import_bad_project_404(self, admin_s):
        r = admin_s.post(f"{API}/units/import", json={
            "project_id": "does-not-exist",
            "rows": [{"tower": "X", "floor": 1, "unit_no": "X-1", "config": "2BHK"}],
        })
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# 7. Unit edit permission
# ---------------------------------------------------------------------------
class TestUnitEdit:
    def test_executive_patch_unit_403(self, priya_s, admin_s, some_project_id):
        units = admin_s.get(f"{API}/units", params={"project_id": some_project_id}).json()
        if not units:
            pytest.skip("no units")
        r = priya_s.patch(f"{API}/units/{units[0]['id']}", json={"status": "held"})
        assert r.status_code == 403

    def test_admin_patch_unit_ok(self, admin_s, some_project_id):
        units = admin_s.get(f"{API}/units", params={"project_id": some_project_id}).json()
        if not units:
            pytest.skip("no units")
        original_status = units[0]["status"]
        r = admin_s.patch(f"{API}/units/{units[0]['id']}", json={"status": "held"})
        assert r.status_code == 200
        assert r.json()["status"] == "held"
        # restore
        admin_s.patch(f"{API}/units/{units[0]['id']}", json={"status": original_status})


# ---------------------------------------------------------------------------
# 8. Project edit
# ---------------------------------------------------------------------------
class TestProjectEdit:
    def test_admin_patch_project(self, admin_s):
        pr = admin_s.post(f"{API}/projects", json={"name": f"TEST_PROJ_{uuid.uuid4().hex[:6]}", "location": "Origin"})
        pid = pr.json()["id"]
        try:
            r = admin_s.patch(f"{API}/projects/{pid}", json={"name": "TEST_PROJ_renamed", "location": "New"})
            assert r.status_code == 200
            assert r.json()["name"] == "TEST_PROJ_renamed"
            assert r.json()["location"] == "New"
        finally:
            admin_s.delete(f"{API}/projects/{pid}")

    def test_manager_patch_project(self, admin_s, manager_s):
        pr = admin_s.post(f"{API}/projects", json={"name": f"TEST_PROJ_{uuid.uuid4().hex[:6]}", "location": "X"})
        pid = pr.json()["id"]
        try:
            r = manager_s.patch(f"{API}/projects/{pid}", json={"name": "TEST_PROJ_mgr_edit", "location": "Y"})
            assert r.status_code == 200
        finally:
            admin_s.delete(f"{API}/projects/{pid}")

    def test_executive_patch_project_403(self, admin_s, priya_s):
        pr = admin_s.post(f"{API}/projects", json={"name": f"TEST_PROJ_{uuid.uuid4().hex[:6]}", "location": "X"})
        pid = pr.json()["id"]
        try:
            r = priya_s.patch(f"{API}/projects/{pid}", json={"name": "hack", "location": "Y"})
            assert r.status_code == 403
        finally:
            admin_s.delete(f"{API}/projects/{pid}")


# ---------------------------------------------------------------------------
# 9. Twilio + settings regression smoke
# ---------------------------------------------------------------------------
class TestRegression:
    def test_twilio_status_configured(self, admin_s):
        r = admin_s.get(f"{API}/twilio/status")
        assert r.status_code == 200
        d = r.json()
        assert d["configured"] is True

    def test_users_me_patch_self_phone(self, priya_s):
        r = priya_s.patch(f"{API}/users/me", json={"phone": "+15005550006"})
        assert r.status_code == 200
        assert r.json()["phone"] == "+15005550006"

    def test_auth_me(self, priya_s):
        r = priya_s.get(f"{API}/auth/me")
        assert r.status_code == 200
        assert r.json()["email"] == "priya@tasko.com"
