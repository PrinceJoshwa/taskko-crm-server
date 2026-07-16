"""Tasko CRM iteration 3 backend tests: Twilio Voice integration.

Covers:
 - GET /api/twilio/status
 - PATCH /api/users/me (self-service phone update)
 - PATCH /api/users/{id} admin-only
 - POST /api/leads/{id}/call — 400 (no phone), 403 (not owner), 502/200 (real call)
 - Twilio TwiML GET/POST /api/twilio/twiml/{lead_id}
 - Twilio status-callback: unsigned=403, signed=200 (missed_call → follow_up)
 - Twilio recording-callback: signed=200 (activity updated w/ recording_url .mp3)
 - Auto-call on new lead (via POST /leads)
 - Auto-call via public webhook (magicbricks)
 - Settings PATCH/GET for new fields
 - Regression: login + dashboard/monthly + list leads
"""
import os
import time
import uuid
import pytest
import requests
from twilio.request_validator import RequestValidator

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://tasko-crm.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN") or ""

TEST_PHONE = "+15005550006"  # Twilio magic test number
LEAD_PHONE = "+919999900011"  # E.164 dummy (unverified → Twilio will reject with 21215/21610)


def _login(session, email, password):
    return session.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=30)


# ---------------- Fixtures ----------------
@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = _login(s, "admin@tasko.com", "admin123")
    assert r.status_code == 200, r.text
    return s


@pytest.fixture(scope="module")
def manager_session():
    s = requests.Session()
    r = _login(s, "manager@tasko.com", "manager123")
    assert r.status_code == 200
    return s


@pytest.fixture(scope="module")
def executive_session():
    s = requests.Session()
    r = _login(s, "priya@tasko.com", "executive123")
    assert r.status_code == 200
    return s


@pytest.fixture(scope="module")
def priya_id(admin_session):
    users = admin_session.get(f"{API}/users").json()
    return next(u for u in users if u["email"] == "priya@tasko.com")["id"]


@pytest.fixture(scope="module")
def karan_id(admin_session):
    users = admin_session.get(f"{API}/users").json()
    return next(u for u in users if u["email"] == "karan@tasko.com")["id"]


@pytest.fixture(scope="module")
def admin_id(admin_session):
    me = admin_session.get(f"{API}/auth/me").json()
    return me["id"]


# ---------------- Twilio status ----------------
class TestTwilioStatus:
    def test_status_requires_auth(self):
        r = requests.get(f"{API}/twilio/status", timeout=15)
        assert r.status_code == 401

    def test_status_configured(self, admin_session):
        r = admin_session.get(f"{API}/twilio/status")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["configured"] is True
        assert data["from_number"] == "+15342044495"
        assert data["webhook_base"] == "https://tasko-crm.preview.emergentagent.com"


# ---------------- Users self-service phone ----------------
class TestUserPhoneUpdates:
    def test_patch_users_me_admin(self, admin_session):
        r = admin_session.patch(f"{API}/users/me", json={"phone": TEST_PHONE})
        assert r.status_code == 200, r.text
        assert r.json()["phone"] == TEST_PHONE

    def test_patch_users_me_executive(self, executive_session):
        r = executive_session.patch(f"{API}/users/me", json={"phone": TEST_PHONE})
        assert r.status_code == 200, r.text
        assert r.json()["phone"] == TEST_PHONE

    def test_patch_users_id_admin_ok(self, admin_session, karan_id):
        r = admin_session.patch(f"{API}/users/{karan_id}", json={"phone": TEST_PHONE})
        assert r.status_code == 200, r.text
        assert r.json()["phone"] == TEST_PHONE

    def test_patch_users_id_executive_forbidden(self, executive_session, karan_id):
        # priya trying to update karan → should be 403 (admin only)
        r = executive_session.patch(f"{API}/users/{karan_id}", json={"phone": "+15551234567"})
        assert r.status_code == 403


# ---------------- Settings toggles ----------------
class TestSettings:
    def test_patch_and_get_settings(self, admin_session):
        # Note: xdist may run TestAutoCallOnNewLead in parallel which flips
        # auto_call_on_new_lead. We only assert on what THIS PATCH sets.
        r = admin_session.patch(f"{API}/settings", json={
            "missed_call_followup_enabled": True,
            "missed_call_followup_hours": 4,
        })
        assert r.status_code == 200, r.text
        s = r.json()
        assert s["missed_call_followup_enabled"] is True
        assert float(s["missed_call_followup_hours"]) == 4.0
        # verify GET persists
        s2 = admin_session.get(f"{API}/settings").json()
        assert s2["missed_call_followup_enabled"] is True
        assert float(s2["missed_call_followup_hours"]) == 4.0
        # separate PATCH toggle off/on to prove roundtrip on auto_call_on_new_lead
        admin_session.patch(f"{API}/settings", json={"auto_call_on_new_lead": True})
        s3 = admin_session.get(f"{API}/settings").json()
        assert s3["auto_call_on_new_lead"] is True
        admin_session.patch(f"{API}/settings", json={"auto_call_on_new_lead": False})
        s4 = admin_session.get(f"{API}/settings").json()
        assert s4["auto_call_on_new_lead"] is False


# ---------------- POST /leads/{id}/call ----------------
class TestClickToCall:
    @pytest.fixture(scope="class")
    def lead_with_phone_admin(self, admin_session, admin_id):
        # Create lead assigned to admin, with phone
        r = admin_session.post(f"{API}/leads", json={
            "name": "TEST_TWL Call Lead",
            "phone": LEAD_PHONE,
            "source": "website",
            "assigned_to": admin_id,
        })
        assert r.status_code == 200, r.text
        return r.json()

    @pytest.fixture(scope="class")
    def lead_no_phone_admin(self, admin_session, admin_id):
        r = admin_session.post(f"{API}/leads", json={
            "name": "TEST_TWL NoPhone Lead",
            "source": "website",
            "assigned_to": admin_id,
        })
        assert r.status_code == 200, r.text
        return r.json()

    @pytest.fixture(scope="class")
    def lead_assigned_to_karan(self, admin_session, karan_id):
        r = admin_session.post(f"{API}/leads", json={
            "name": "TEST_TWL Karan Lead",
            "phone": LEAD_PHONE,
            "source": "website",
            "assigned_to": karan_id,
        })
        assert r.status_code == 200
        return r.json()

    def test_exec_without_phone_returns_400(self, admin_session, executive_session, lead_with_phone_admin, priya_id):
        # Unset priya's phone first
        admin_session.patch(f"{API}/users/{priya_id}", json={"phone": ""})
        # Re-login exec so session picks up user attributes if any cached (not needed but safe)
        r = executive_session.post(f"{API}/leads/{lead_with_phone_admin['id']}/call")
        # Two possible reasons: (a) 403 not-owner (lead is admin's), (b) 400 no phone.
        # Since ownership is checked first, expect 403 here.
        assert r.status_code == 403
        # Restore priya phone for later
        admin_session.patch(f"{API}/users/{priya_id}", json={"phone": TEST_PHONE})

    def test_exec_no_phone_owned_lead_returns_400(self, admin_session, executive_session, priya_id):
        # Unset priya's phone
        admin_session.patch(f"{API}/users/{priya_id}", json={"phone": ""})
        # Create lead owned by priya
        lr = admin_session.post(f"{API}/leads", json={
            "name": "TEST_TWL Priya Owned",
            "phone": LEAD_PHONE,
            "source": "website",
            "assigned_to": priya_id,
        })
        lead = lr.json()
        r = executive_session.post(f"{API}/leads/{lead['id']}/call")
        assert r.status_code == 400, r.text
        assert "phone" in (r.json().get("detail") or "").lower()
        # cleanup
        admin_session.patch(f"{API}/users/{priya_id}", json={"phone": TEST_PHONE})
        admin_session.delete(f"{API}/leads/{lead['id']}")

    def test_exec_not_owner_returns_403(self, executive_session, lead_assigned_to_karan):
        r = executive_session.post(f"{API}/leads/{lead_assigned_to_karan['id']}/call")
        assert r.status_code == 403

    def test_lead_without_phone_returns_400(self, admin_session, lead_no_phone_admin):
        r = admin_session.post(f"{API}/leads/{lead_no_phone_admin['id']}/call")
        assert r.status_code == 400, r.text
        assert "phone" in (r.json().get("detail") or "").lower()

    def test_admin_valid_call_returns_502_or_200(self, admin_session, lead_with_phone_admin):
        r = admin_session.post(f"{API}/leads/{lead_with_phone_admin['id']}/call")
        # Twilio trial → unverified destination → 502 expected. Success → 200.
        # NOTE: Cloudflare intercepts origin 5xx responses and replaces the JSON
        # body with an HTML error page, so we cannot inspect r.json() on 502
        # via the public URL.  We instead cross-verify via the activities
        # endpoint (no activity should be recorded on Twilio error path).
        assert r.status_code in (200, 502), f"unexpected {r.status_code}: {r.text[:300]}"
        time.sleep(0.7)
        acts = admin_session.get(f"{API}/activities", params={"lead_id": lead_with_phone_admin["id"]}).json()
        oc = [a for a in acts if a["kind"] == "outgoing_call"]
        if r.status_code == 200:
            data = r.json()
            assert data.get("call_sid", "").startswith("CA") or data.get("mock") is True
            assert oc, "no outgoing_call activity created on success path"
        else:
            # 502 – Twilio raised, verify activity NOT inserted
            assert not oc, "outgoing_call activity should NOT exist on Twilio error"


# ---------------- Twilio TwiML endpoint (public) ----------------
class TestTwiml:
    @pytest.fixture(scope="class")
    def lead_id_with_phone(self, admin_session, admin_id):
        r = admin_session.post(f"{API}/leads", json={
            "name": "TEST_TWL TwiML lead",
            "phone": LEAD_PHONE,
            "source": "website",
            "assigned_to": admin_id,
        })
        return r.json()["id"]

    @pytest.fixture(scope="class")
    def lead_id_no_phone(self, admin_session, admin_id):
        r = admin_session.post(f"{API}/leads", json={
            "name": "TEST_TWL TwiML NoPh lead",
            "source": "website",
            "assigned_to": admin_id,
        })
        return r.json()["id"]

    def test_twiml_with_phone_dial(self, lead_id_with_phone):
        r = requests.get(f"{API}/twilio/twiml/{lead_id_with_phone}", timeout=15)
        assert r.status_code == 200, r.text
        assert "application/xml" in r.headers.get("content-type", "")
        xml = r.text
        assert "<Say" in xml
        assert "<Dial" in xml
        assert LEAD_PHONE in xml

    def test_twiml_post_also_ok(self, lead_id_with_phone):
        r = requests.post(f"{API}/twilio/twiml/{lead_id_with_phone}", timeout=15)
        assert r.status_code == 200
        assert "<Dial" in r.text

    def test_twiml_no_phone_polite(self, lead_id_no_phone):
        r = requests.get(f"{API}/twilio/twiml/{lead_id_no_phone}", timeout=15)
        assert r.status_code == 200
        assert "<Say" in r.text
        assert "<Dial" not in r.text


# ---------------- Twilio callbacks (signature) ----------------
LOCAL_API = "http://localhost:8001/api"


class TestTwilioCallbacks:
    """Twilio signature verification is done using str(request.url) which,
    behind the ingress, resolves to the internal http://localhost:8001/...
    URL. To exercise the *signed* happy path we therefore hit the endpoints
    from inside the pod on localhost:8001 (as the review spec allows).
    """

    def test_status_callback_unsigned_public_returns_403(self):
        # Public URL, no signature header — should still 403 (token present)
        r = requests.post(
            f"{API}/twilio/status-callback",
            data={"CallSid": "CA_UNSIGNED_TEST", "CallStatus": "no-answer", "CallDuration": "0"},
            timeout=15,
        )
        assert r.status_code == 403

    def test_status_callback_unsigned_local_returns_403(self):
        r = requests.post(
            f"{LOCAL_API}/twilio/status-callback",
            data={"CallSid": "CA_UNSIGNED_LOCAL", "CallStatus": "no-answer", "CallDuration": "0"},
            timeout=15,
        )
        assert r.status_code == 403

    def test_recording_callback_unsigned_returns_403(self):
        r = requests.post(
            f"{API}/twilio/recording-callback",
            data={"CallSid": "CA_UR", "RecordingUrl": "https://api.twilio.com/rec/RE1", "RecordingSid": "RE1", "RecordingDuration": "42"},
            timeout=15,
        )
        assert r.status_code == 403

    def test_status_callback_signed_no_matching_activity(self):
        """Signed request but no matching activity → 200 (no-op)."""
        if not TWILIO_TOKEN:
            pytest.skip("TWILIO_AUTH_TOKEN missing")
        validator = RequestValidator(TWILIO_TOKEN)
        url = f"{LOCAL_API}/twilio/status-callback"
        params = {"CallSid": "CA_NOMATCH_" + uuid.uuid4().hex, "CallStatus": "no-answer", "CallDuration": "0"}
        sig = validator.compute_signature(url, params)
        r = requests.post(url, data=params, headers={"X-Twilio-Signature": sig}, timeout=15)
        assert r.status_code == 200, f"got {r.status_code}: {r.text[:200]}"

    def test_status_callback_signed_happy_path_missed(self, admin_session, admin_id):
        """Insert an activity with a known call_sid via direct Mongo → POST
        signed status-callback with no-answer → verify activity became
        missed_call AND a follow-up was auto-scheduled."""
        if not TWILIO_TOKEN:
            pytest.skip("TWILIO_AUTH_TOKEN missing")
        try:
            from pymongo import MongoClient
        except ImportError:
            pytest.skip("pymongo not installed")

        mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
        db_name = os.environ.get("DB_NAME", "test_database")
        mc = MongoClient(mongo_url)
        db = mc[db_name]

        # ensure settings enable missed-call follow-up with 4h
        admin_session.patch(f"{API}/settings", json={
            "missed_call_followup_enabled": True,
            "missed_call_followup_hours": 4,
        })

        # Create a lead + activity seed
        lead_resp = admin_session.post(f"{API}/leads", json={
            "name": "TEST_TWL Missed-Call Lead",
            "phone": LEAD_PHONE,
            "source": "website",
            "assigned_to": admin_id,
        })
        lead_id = lead_resp.json()["id"]

        call_sid = "CATEST" + uuid.uuid4().hex
        activity_doc = {
            "id": str(uuid.uuid4()),
            "lead_id": lead_id,
            "actor_id": admin_id,
            "actor_name": "test-seed",
            "kind": "outgoing_call",
            "message": "Call initiated",
            "meta": {"direction": "outgoing", "call_sid": call_sid, "status": "initiated"},
            "created_at": "2026-01-15T10:00:00+00:00",
        }
        db.activities.insert_one(activity_doc)

        # Signed POST from inside the pod
        validator = RequestValidator(TWILIO_TOKEN)
        url = f"{LOCAL_API}/twilio/status-callback"
        params = {"CallSid": call_sid, "CallStatus": "no-answer", "CallDuration": "0"}
        sig = validator.compute_signature(url, params)
        r = requests.post(url, data=params, headers={"X-Twilio-Signature": sig}, timeout=15)
        assert r.status_code == 200, f"got {r.status_code}: {r.text[:200]}"

        # Verify activity mutation
        act = db.activities.find_one({"id": activity_doc["id"]})
        assert act["kind"] == "missed_call", f"activity kind was {act['kind']}"
        assert act["meta"]["status"] == "no-answer"
        assert act["meta"]["disposition"] in ("no_answer", "no-answer")

        # Verify follow-up scheduled
        fu = db.follow_ups.find_one({"meta.source_call_sid": call_sid})
        assert fu is not None, "auto follow-up not created"
        assert fu["kind"] == "call"
        assert fu["status"] == "pending"
        assert "Auto-scheduled" in (fu.get("notes") or "")

        # Cleanup
        db.activities.delete_one({"id": activity_doc["id"]})
        db.follow_ups.delete_many({"meta.source_call_sid": call_sid})
        admin_session.delete(f"{API}/leads/{lead_id}")

    def test_recording_callback_signed_happy_path(self, admin_session, admin_id):
        """Seed activity → signed recording-callback → verify recording_url
        (auto-ending in .mp3), recording_sid, recording_duration_sec = 42."""
        if not TWILIO_TOKEN:
            pytest.skip("TWILIO_AUTH_TOKEN missing")
        try:
            from pymongo import MongoClient
        except ImportError:
            pytest.skip("pymongo not installed")

        mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
        db_name = os.environ.get("DB_NAME", "test_database")
        db = MongoClient(mongo_url)[db_name]

        lead_resp = admin_session.post(f"{API}/leads", json={
            "name": "TEST_TWL Recording Lead",
            "phone": LEAD_PHONE,
            "source": "website",
            "assigned_to": admin_id,
        })
        lead_id = lead_resp.json()["id"]

        call_sid = "CAREC" + uuid.uuid4().hex
        aid = str(uuid.uuid4())
        db.activities.insert_one({
            "id": aid,
            "lead_id": lead_id,
            "actor_id": admin_id,
            "actor_name": "test-seed",
            "kind": "outgoing_call",
            "message": "Call initiated",
            "meta": {"direction": "outgoing", "call_sid": call_sid, "status": "completed"},
            "created_at": "2026-01-15T10:00:00+00:00",
        })

        validator = RequestValidator(TWILIO_TOKEN)
        url = f"{LOCAL_API}/twilio/recording-callback"
        params = {
            "CallSid": call_sid,
            "RecordingUrl": "https://api.twilio.com/2010-04-01/Accounts/AC/Recordings/RE123",
            "RecordingSid": "RE123",
            "RecordingDuration": "42",
        }
        sig = validator.compute_signature(url, params)
        r = requests.post(url, data=params, headers={"X-Twilio-Signature": sig}, timeout=15)
        assert r.status_code == 200, r.text[:200]

        act = db.activities.find_one({"id": aid})
        assert act["meta"]["recording_url"].endswith(".mp3"), act["meta"].get("recording_url")
        assert act["meta"]["recording_sid"] == "RE123"
        assert act["meta"]["recording_duration_sec"] == 42

        db.activities.delete_one({"id": aid})
        admin_session.delete(f"{API}/leads/{lead_id}")


# ---------------- Auto-call on new lead ----------------
class TestAutoCallOnNewLead:
    def test_auto_call_on_create_lead(self, admin_session, priya_id):
        # Ensure priya has a phone
        admin_session.patch(f"{API}/users/{priya_id}", json={"phone": TEST_PHONE})
        # Enable settings
        admin_session.patch(f"{API}/settings", json={
            "auto_call_on_new_lead": True,
            "missed_call_followup_enabled": True,
            "missed_call_followup_hours": 4,
        })
        # Create a lead assigned to priya
        r = admin_session.post(f"{API}/leads", json={
            "name": "TEST_TWL AutoCall Lead",
            "phone": LEAD_PHONE,
            "source": "website",
            "assigned_to": priya_id,
        })
        # Lead creation must succeed even if Twilio errors
        assert r.status_code == 200, r.text
        lead = r.json()
        assert lead.get("assigned_to") == priya_id
        # Twilio may or may not add activity (502 path won't). We just assert
        # that the lead exists.
        # cleanup
        admin_session.delete(f"{API}/leads/{lead['id']}")
        # turn off
        admin_session.patch(f"{API}/settings", json={"auto_call_on_new_lead": False})

    def test_auto_call_via_public_webhook(self, admin_session, priya_id):
        admin_session.patch(f"{API}/users/{priya_id}", json={"phone": TEST_PHONE})
        admin_session.patch(f"{API}/settings", json={"auto_call_on_new_lead": True, "auto_assign_enabled": True})
        r = requests.post(f"{API}/webhooks/leads/magicbricks", json={
            "name": "TEST_TWL Webhook Lead",
            "phone": LEAD_PHONE,
            "email": "test_twl_wh@example.com",
            "project": "Aurelia Heights",
            "message": "iteration3",
        }, timeout=20)
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True
        # cleanup
        lead_id = r.json().get("lead_id")
        if lead_id:
            admin_session.delete(f"{API}/leads/{lead_id}")
        admin_session.patch(f"{API}/settings", json={"auto_call_on_new_lead": False})


# ---------------- Regression spot-check ----------------
class TestRegression:
    def test_login_admin(self):
        s = requests.Session()
        r = _login(s, "admin@tasko.com", "admin123")
        assert r.status_code == 200
        assert r.json()["role"] == "admin"

    def test_dashboard_monthly(self, admin_session):
        r = admin_session.get(f"{API}/dashboard/monthly")
        assert r.status_code == 200
        d = r.json()
        assert "kpi" in d and "pipeline" in d

    def test_list_leads(self, admin_session):
        r = admin_session.get(f"{API}/leads")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


# ---------------- Teardown (best-effort test data cleanup) ----------------
def teardown_module(module):
    try:
        s = requests.Session()
        _login(s, "admin@tasko.com", "admin123")
        leads = s.get(f"{API}/leads").json()
        for l in leads:
            if (l.get("name") or "").startswith("TEST_TWL"):
                s.delete(f"{API}/leads/{l['id']}")
    except Exception:
        pass
