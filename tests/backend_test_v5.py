"""
Iteration 5 backend tests — Notifications + Admin EOD summary + email.

Covers:
- /api/notifications role-scoped listing for admin/manager/executive
- POST /api/notifications/refresh dedupe behavior
- Mark-one-read / read-all endpoints
- /api/admin/eod-summary schema + role gating (admin only, 403 for others)
- /api/admin/eod-email/send (Resend testing mode ok — non-empty errors[] acceptable)
- Regression: creating a lead assigned to executive spawns lead_assigned notif
- Regression: creating a follow-up spawns followup_due notif
- Regression: dashboard/leads/projects/follow-ups/site-visits/twilio endpoints still 200
"""
import os
import pytest
import requests
import uuid

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL")
if not BASE_URL:
    # Load from /app/frontend/.env
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip()
                    break
    except Exception:
        pass
if not BASE_URL:
    BASE_URL = "http://localhost:8001"
BASE_URL = BASE_URL.rstrip("/")
API = f"{BASE_URL}/api"


def _login(session: requests.Session, email: str, password: str) -> None:
    r = session.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=15)
    assert r.status_code == 200, f"login failed for {email}: {r.status_code} {r.text}"


@pytest.fixture(scope="module")
def admin_sess():
    s = requests.Session()
    _login(s, "admin@tasko.com", "admin123")
    return s


@pytest.fixture(scope="module")
def manager_sess():
    s = requests.Session()
    _login(s, "manager@tasko.com", "manager123")
    return s


@pytest.fixture(scope="module")
def priya_sess():
    s = requests.Session()
    _login(s, "priya@tasko.com", "executive123")
    return s


@pytest.fixture(scope="module")
def priya_user(admin_sess):
    r = admin_sess.get(f"{API}/users", timeout=10)
    assert r.status_code == 200
    for u in r.json():
        if u.get("email") == "priya@tasko.com":
            return u
    pytest.fail("priya user not found")


# ---------------------------------------------------------------------------
# Notifications role-scope
# ---------------------------------------------------------------------------
MANAGER_SCOPE_TYPES = {"team_overdue", "stale_lead", "negotiation_pending", "exec_no_activity"}


class TestNotificationsRefresh:
    def test_refresh_returns_created(self, admin_sess):
        r = admin_sess.post(f"{API}/notifications/refresh", timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "created" in data
        assert isinstance(data["created"], int)
        assert data["created"] >= 0

    def test_refresh_is_idempotent_dedup(self, admin_sess):
        # First refresh
        r1 = admin_sess.post(f"{API}/notifications/refresh", timeout=30)
        # Second immediately after — dedupe_key logic should skip already-seen aggregates
        # Count manager-scope items before/after; must not double up
        before = admin_sess.get(f"{API}/notifications", timeout=10).json()
        r2 = admin_sess.post(f"{API}/notifications/refresh", timeout=30)
        assert r2.status_code == 200
        after = admin_sess.get(f"{API}/notifications", timeout=10).json()
        # dedup counts for aggregate types should be equal
        def counts(items, t):
            return sum(1 for i in items if i.get("type") == t)
        for t in MANAGER_SCOPE_TYPES:
            assert counts(after["items"], t) == counts(before["items"], t), (
                f"dedupe failed for {t}: before={counts(before['items'], t)} after={counts(after['items'], t)}"
            )


class TestNotificationsScope:
    def test_admin_sees_manager_scope(self, admin_sess):
        # Ensure aggregates exist
        admin_sess.post(f"{API}/notifications/refresh", timeout=30)
        r = admin_sess.get(f"{API}/notifications", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert "items" in data and "unread" in data
        assert data["unread"] >= 0
        types = {i.get("type") for i in data["items"]}
        # At least one manager-scope aggregate should be visible to admin
        assert types & MANAGER_SCOPE_TYPES, (
            f"admin should see manager-scope aggregates, got types={types}"
        )

    def test_manager_sees_manager_scope_only(self, manager_sess, admin_sess):
        admin_sess.post(f"{API}/notifications/refresh", timeout=30)
        r = manager_sess.get(f"{API}/notifications", timeout=10)
        assert r.status_code == 200
        for item in r.json()["items"]:
            # Manager may see personal (user_id == manager) or role_scope==manager
            # but never admin-only (role_scope=="admin")
            assert item.get("role_scope") != "admin", (
                f"manager saw admin-scope notif: {item}"
            )

    def test_executive_never_sees_manager_scope(self, priya_sess, priya_user, admin_sess):
        admin_sess.post(f"{API}/notifications/refresh", timeout=30)
        r = priya_sess.get(f"{API}/notifications", timeout=10)
        assert r.status_code == 200
        for item in r.json()["items"]:
            assert item.get("type") not in MANAGER_SCOPE_TYPES, (
                f"executive saw manager-scope notif: {item}"
            )
            # Executive must only see personal notifs targeted at them
            assert item.get("role_scope") in (None, "executive"), item
            assert item.get("user_id") == priya_user["id"], item


class TestNotificationsMarkRead:
    def test_mark_one_read_decrements(self, admin_sess):
        # Ensure something is unread
        admin_sess.post(f"{API}/notifications/refresh", timeout=30)
        # If nothing unread, create a personal one via creating a lead? — simpler: pick any unread from list
        listing = admin_sess.get(f"{API}/notifications", timeout=10).json()
        unread_items = [i for i in listing["items"] if not i.get("read")]
        if not unread_items:
            pytest.skip("no unread notifications to test single-read decrement")
        before_unread = listing["unread"]
        nid = unread_items[0]["id"]
        r = admin_sess.post(f"{API}/notifications/{nid}/read", timeout=10)
        assert r.status_code == 200
        assert r.json().get("ok") is True

        after = admin_sess.get(f"{API}/notifications", timeout=10).json()
        assert after["unread"] == before_unread - 1
        # The specific item is now read=True
        item = next((i for i in after["items"] if i.get("id") == nid), None)
        assert item is not None
        assert item.get("read") is True

    def test_read_all_zeroes(self, admin_sess):
        admin_sess.post(f"{API}/notifications/refresh", timeout=30)
        r = admin_sess.post(f"{API}/notifications/read-all", timeout=10)
        assert r.status_code == 200
        assert r.json().get("ok") is True
        listing = admin_sess.get(f"{API}/notifications", timeout=10).json()
        assert listing["unread"] == 0


# ---------------------------------------------------------------------------
# Lead assignment → notification
# ---------------------------------------------------------------------------
class TestLeadCreationSpawnsNotification:
    def test_lead_assigned_notif(self, admin_sess, priya_sess, priya_user):
        # Read-all first so we can detect the fresh notification
        priya_sess.post(f"{API}/notifications/read-all", timeout=10)
        unique = uuid.uuid4().hex[:6]
        payload = {
            "name": f"TEST_notif_lead_{unique}",
            "phone": f"9000{unique[:6]}",
            "email": f"testnotif{unique}@example.com",
            "source": "website",
            "assigned_to": priya_user["id"],
        }
        r = admin_sess.post(f"{API}/leads", json=payload, timeout=15)
        assert r.status_code == 200, r.text
        lead = r.json()
        assert lead.get("assigned_to") == priya_user["id"]

        # Priya should now see a lead_assigned notif
        listing = priya_sess.get(f"{API}/notifications", timeout=10).json()
        matching = [
            i for i in listing["items"]
            if i.get("type") == "lead_assigned"
            and (i.get("meta") or {}).get("lead_id") == lead["id"]
        ]
        assert matching, f"lead_assigned notification not found for lead {lead['id']}. items={listing['items'][:5]}"
        assert matching[0].get("user_id") == priya_user["id"]

        # cleanup
        admin_sess.delete(f"{API}/leads/{lead['id']}", timeout=10)


class TestFollowupSpawnsNotification:
    def test_followup_due_notif(self, admin_sess, priya_sess, priya_user):
        # Create a lead for priya, then a follow-up on that lead
        unique = uuid.uuid4().hex[:6]
        lead_payload = {
            "name": f"TEST_fu_lead_{unique}",
            "phone": f"9111{unique[:6]}",
            "email": f"fu{unique}@example.com",
            "source": "website",
            "assigned_to": priya_user["id"],
        }
        r = admin_sess.post(f"{API}/leads", json=lead_payload, timeout=15)
        assert r.status_code == 200
        lead_id = r.json()["id"]

        priya_sess.post(f"{API}/notifications/read-all", timeout=10)

        from datetime import datetime, timedelta, timezone
        due_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        fu_payload = {
            "lead_id": lead_id,
            "kind": "call",
            "due_at": due_at,
            "assigned_to": priya_user["id"],
            "note": "TEST_fu",
        }
        r2 = admin_sess.post(f"{API}/follow-ups", json=fu_payload, timeout=15)
        assert r2.status_code == 200, r2.text
        fu = r2.json()

        listing = priya_sess.get(f"{API}/notifications", timeout=10).json()
        matching = [
            i for i in listing["items"]
            if i.get("type") == "followup_due"
            and (i.get("meta") or {}).get("followup_id") == fu["id"]
        ]
        assert matching, f"followup_due notification not found. items={listing['items'][:5]}"

        # cleanup
        admin_sess.delete(f"{API}/leads/{lead_id}", timeout=10)


# ---------------------------------------------------------------------------
# EOD summary + email
# ---------------------------------------------------------------------------
EOD_KEYS = ("date", "generated_at", "followups", "milestones", "calls", "top_execs")


class TestEODSummary:
    def test_admin_get_summary(self, admin_sess):
        r = admin_sess.get(f"{API}/admin/eod-summary", timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        for k in EOD_KEYS:
            assert k in d, f"missing {k}"
        assert "due_today" in d["followups"]
        assert "overdue" in d["followups"]
        for k in ("bookings", "new_leads", "site_visits_completed"):
            assert k in d["milestones"], f"missing milestones.{k}"
        for k in ("total", "connected", "missed", "talk_time_sec"):
            assert k in d["calls"], f"missing calls.{k}"
        assert isinstance(d["top_execs"], list)

    def test_manager_forbidden(self, manager_sess):
        r = manager_sess.get(f"{API}/admin/eod-summary", timeout=10)
        assert r.status_code == 403, r.text

    def test_executive_forbidden(self, priya_sess):
        r = priya_sess.get(f"{API}/admin/eod-summary", timeout=10)
        assert r.status_code == 403, r.text


class TestEODEmail:
    def test_admin_send_email_schema(self, admin_sess):
        r = admin_sess.post(f"{API}/admin/eod-email/send", timeout=60)
        assert r.status_code == 200, r.text
        d = r.json()
        assert "sent" in d
        assert "date" in d
        # errors[] may be non-empty in Resend testing mode — that's acceptable per spec
        assert "errors" in d
        assert isinstance(d["errors"], list)
        # If errors are present, each should have expected shape
        for err in d["errors"]:
            assert "email" in err and "error" in err

    def test_manager_forbidden(self, manager_sess):
        r = manager_sess.post(f"{API}/admin/eod-email/send", timeout=15)
        assert r.status_code == 403

    def test_executive_forbidden(self, priya_sess):
        r = priya_sess.post(f"{API}/admin/eod-email/send", timeout=15)
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Regression — existing endpoints still 200
# ---------------------------------------------------------------------------
class TestRegression:
    @pytest.mark.parametrize("path", [
        "/dashboard/monthly",
        "/dashboard/revenue-breakdown",
        "/leads",
        "/projects",
        "/follow-ups",
        "/site-visits",
        "/twilio/status",
        "/users",
        "/auth/me",
    ])
    def test_get_200(self, admin_sess, path):
        r = admin_sess.get(f"{API}{path}", timeout=15)
        assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:200]}"
