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
