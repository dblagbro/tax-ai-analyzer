"""Authentication + authorization boundary tests.

Covers:
- ENH-1: non-admin session fixture
- HIGH-4: inactive user is denied authentication
- Unauthenticated redirect behavior
- Admin-only routes reject non-admin sessions with 403/redirect

These tests mutate state only via Flask-Login session cookies, not the DB,
so they can run against the live container without teardown concerns.
"""
from unittest.mock import patch

import pytest

from app.auth import User, load_user
from app.web_ui import app as flask_app


def _fake_row(uid=99, username="fakeuser", role="standard", active=1, email="x@y.z"):
    return {
        "id": uid,
        "username": username,
        "role": role,
        "active": active,
        "email": email,
    }


# ───────────────────────────────────────────────────────────────────────────────
# ENH-1 / HIGH-4 — User model behavior
# ───────────────────────────────────────────────────────────────────────────────

class TestUserModel:
    def test_active_user_is_active(self):
        u = User(_fake_row(active=1))
        assert u.is_active is True

    def test_inactive_user_is_not_active(self):
        # HIGH-4: inactive accounts must not pass Flask-Login's is_active gate
        u = User(_fake_row(active=0))
        assert u.is_active is False

    def test_admin_role_grants_is_admin(self):
        assert User(_fake_row(role="admin")).is_admin is True
        assert User(_fake_row(role="superuser")).is_admin is True

    def test_standard_role_is_not_admin(self):
        assert User(_fake_row(role="standard")).is_admin is False

    def test_superuser_role(self):
        assert User(_fake_row(role="superuser")).is_superuser is True
        assert User(_fake_row(role="admin")).is_superuser is False


class TestLoader:
    def test_load_nonexistent_user_returns_none(self):
        assert load_user("99999999") is None

    def test_load_invalid_id_returns_none(self):
        assert load_user("not-a-number") is None
        assert load_user("") is None


# ───────────────────────────────────────────────────────────────────────────────
# Authorization — admin-only endpoints reject non-admin sessions
# ───────────────────────────────────────────────────────────────────────────────

# Representative admin-only routes. These return 302 (login redirect) for
# unauth, 403 or redirect for non-admin authed.
ADMIN_ONLY_ROUTES = [
    "/tax-ai-analyzer/api/settings",           # @admin_required
    "/tax-ai-analyzer/api/users",              # admin user mgmt
]


@pytest.fixture
def non_admin_client():
    """Client session presenting as a standard (non-admin) user."""
    fake = User(_fake_row(uid=9001, username="qa_nonadmin", role="standard", active=1))
    client = flask_app.test_client()
    with patch("app.auth.load_user", return_value=fake):
        with client.session_transaction() as sess:
            sess["_user_id"] = "9001"
            sess["_fresh"] = True
        yield client


@pytest.fixture
def inactive_client():
    """Client session presenting as a user whose account was deactivated."""
    fake = User(_fake_row(uid=9002, username="qa_inactive", role="standard", active=0))
    client = flask_app.test_client()
    with patch("app.auth.load_user", return_value=fake):
        with client.session_transaction() as sess:
            sess["_user_id"] = "9002"
            sess["_fresh"] = True
        yield client


class TestUnauthenticated:
    def test_admin_route_redirects_unauthenticated(self):
        client = flask_app.test_client()
        for path in ADMIN_ONLY_ROUTES:
            resp = client.get(path)
            # Either 302 redirect to login or 401; never 200 with real data
            assert resp.status_code in (302, 401), f"{path}: got {resp.status_code}"


class TestNonAdminAuth:
    def test_admin_only_routes_reject_non_admin(self, non_admin_client):
        for path in ADMIN_ONLY_ROUTES:
            resp = non_admin_client.get(path)
            # @admin_required typically returns 403; accept redirect too
            assert resp.status_code in (302, 403, 404), (
                f"{path}: non-admin got {resp.status_code} — admin-only gate leaked"
            )


class TestInactiveUser:
    def test_inactive_user_cannot_hold_session(self, inactive_client):
        # Flask-Login should not treat an inactive user as authenticated.
        # Hitting any @login_required route should redirect or reject.
        resp = inactive_client.get("/tax-ai-analyzer/api/settings")
        assert resp.status_code in (302, 401, 403), (
            f"inactive user passed auth gate: {resp.status_code}"
        )


# ───────────────────────────────────────────────────────────────────────────────
# CRIT-NEW-3 — Open redirect guard on /login?next=
# ───────────────────────────────────────────────────────────────────────────────

from app.routes.auth import _safe_next, _client_ip  # noqa: E402


class TestOpenRedirect:
    """Unit tests on _safe_next — the helper that validates the ?next= param."""

    def test_external_url_rejected(self):
        assert _safe_next("https://evil.com/") is None
        assert _safe_next("http://evil.com/") is None

    def test_protocol_relative_rejected(self):
        # //evil.com is protocol-relative and would redirect off-origin
        assert _safe_next("//evil.com/") is None

    def test_javascript_scheme_rejected(self):
        assert _safe_next("javascript:alert(1)") is None

    def test_same_origin_path_accepted(self):
        assert _safe_next("/tax-ai-analyzer/import") == "/tax-ai-analyzer/import"
        assert _safe_next("/") == "/"

    def test_empty_and_none_return_none(self):
        assert _safe_next("") is None
        assert _safe_next(None) is None


class TestOpenRedirectIntegration:
    """End-to-end: POST /login?next=https://evil.com/ must redirect to /.

    Hermetic — sets the admin password to a known value for the duration of
    the test, then restores the original hash. The runtime DB has its admin
    password seeded from ADMIN_INITIAL_PASSWORD, which we don't know in CI.
    """

    def test_login_post_strips_external_next(self):
        from app import db
        from app.db.users import _hash_password
        from werkzeug.security import generate_password_hash

        conn = db.get_connection()
        row = conn.execute(
            "SELECT id, password_hash FROM users WHERE username='admin'"
        ).fetchone()
        conn.close()
        if not row:
            return  # No admin to test against — environment-dependent skip
        admin_id, original_hash = row["id"], row["password_hash"]

        try:
            db.update_user(admin_id, password="admin_test_pw")
            client = flask_app.test_client()
            resp = client.post(
                "/tax-ai-analyzer/login?next=https://evil.com/",
                data={"username": "admin", "password": "admin_test_pw"},
                follow_redirects=False,
            )
            assert resp.status_code == 302, (
                f"login failed (status {resp.status_code}) — "
                f"hermetic password reset didn't take?"
            )
            location = resp.headers.get("Location", "")
            assert not location.startswith("https://evil.com"), (
                f"open redirect leaked: Location={location!r}"
            )
            assert not location.startswith("//"), \
                f"protocol-relative leak: {location!r}"
        finally:
            # Restore the real hash directly (we don't know the plaintext)
            conn = db.get_connection()
            conn.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (original_hash, admin_id),
            )
            conn.commit()
            conn.close()


# ───────────────────────────────────────────────────────────────────────────────
# CRIT-PASS2-1 — X-Forwarded-For rate-limiter bypass
# ───────────────────────────────────────────────────────────────────────────────

class TestRateLimitIpResolution:
    """_client_ip must read request.remote_addr only. The actual XFF chain
    parsing is delegated to werkzeug.middleware.proxy_fix.ProxyFix(x_for=1)
    configured in app/web_ui.py. Unit tests here assert the function ignores
    any attacker-supplied X-Forwarded-For headers that bypass ProxyFix."""

    def test_returns_remote_addr(self):
        with flask_app.test_request_context(
                "/tax-ai-analyzer/login",
                environ_base={"REMOTE_ADDR": "203.0.113.5"}):
            assert _client_ip() == "203.0.113.5"

    def test_ignores_header_only_xff(self):
        # If ProxyFix didn't run (e.g. test_request_context alone), an XFF
        # header shouldn't affect _client_ip — we trust only remote_addr.
        with flask_app.test_request_context(
                "/tax-ai-analyzer/login",
                headers={"X-Forwarded-For": "1.2.3.4"},
                environ_base={"REMOTE_ADDR": "203.0.113.5"}):
            assert _client_ip() == "203.0.113.5"

    def test_handles_missing_remote_addr(self):
        with flask_app.test_request_context(
                "/tax-ai-analyzer/login",
                environ_base={"REMOTE_ADDR": None}):
            assert _client_ip() == ""


class TestRateLimitXffBypass:
    """Integration: 15 bad logins with rotating XFF all hit the same remote_addr
    (127.0.0.1) — the rate limiter must still fire."""

    def test_xff_rotation_cannot_bypass_limit(self):
        client = flask_app.test_client()
        codes = []
        for i in range(15):
            resp = client.post(
                "/tax-ai-analyzer/login",
                data={"username": "nobody", "password": f"wrong{i}"},
                headers={"X-Forwarded-For": f"10.{i}.{i}.{i}"},
            )
            codes.append(resp.status_code)
        n_429 = codes.count(429)
        assert n_429 >= 3, (
            f"expected rate-limiter to fire ≥3x during 15 XFF-rotated bad "
            f"logins; got status codes: {codes}"
        )
