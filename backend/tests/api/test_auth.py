from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from policy_api.auth.passwords import hash_password
from policy_api.config import Settings
from policy_api.database import create_database_engine, create_session_factory
from policy_api.main import create_app
from policy_api.models import Answer, AnswerCitation, Feedback, Question, Session, User, UserRole, utc_now


def build_settings(database_url: str) -> Settings:
    return Settings(
        app_env="test",
        database_url=database_url,
        session_secret="s" * 32,
        frontend_origins="http://localhost:5173",
        upload_root="./uploads-test",
        model_base_url="https://models.example.test/v1",
        model_api_key="secret-api-key",
        chat_model="chat-test",
        embedding_model="embedding-test",
    )


@pytest.fixture
def auth_app(TEST_DATABASE_URL: str):  # type: ignore[invalid-name]
    engine = create_database_engine(TEST_DATABASE_URL)
    session_factory = create_session_factory(engine)
    with session_factory() as database:
        database.query(Feedback).delete()
        database.query(AnswerCitation).delete()
        database.query(Answer).delete()
        database.query(Question).delete()
        database.query(Session).delete()
        database.query(User).delete()
        database.add_all(
            [
                User(
                    username="employee",
                    password_hash=hash_password("employee-password"),
                    role=UserRole.EMPLOYEE,
                ),
                User(
                    username="admin",
                    password_hash=hash_password("admin-password"),
                    role=UserRole.ADMIN,
                ),
                User(
                    username="hr",
                    password_hash=hash_password("hr-password"),
                    role=UserRole.HR,
                ),
            ]
        )
        database.commit()

    app = create_app(build_settings(TEST_DATABASE_URL), session_factory=session_factory)
    yield app, session_factory
    engine.dispose()


@pytest.mark.anyio
async def test_login_me_and_logout_use_a_secure_session_cookie(auth_app) -> None:
    app, session_factory = auth_app
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post(
            "/api/v1/auth/login",
            json={"username": "employee", "password": "employee-password"},
        )
        me = await client.get("/api/v1/auth/me")
        logout = await client.post("/api/v1/auth/logout")
        after_logout = await client.get("/api/v1/auth/me")

    assert login.status_code == 204
    cookie = login.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" not in cookie
    assert me.json() == {"username": "employee", "role": "employee"}
    assert logout.status_code == 204
    assert after_logout.status_code == 401
    with session_factory() as database:
        assert database.scalar(select(Session.revoked_at)) is not None


@pytest.mark.anyio
async def test_login_failure_is_uniform_for_unknown_user_and_wrong_password(auth_app) -> None:
    app, _session_factory = auth_app
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unknown = await client.post(
            "/api/v1/auth/login", json={"username": "missing", "password": "wrong"}
        )
        incorrect = await client.post(
            "/api/v1/auth/login", json={"username": "employee", "password": "wrong"}
        )

    assert unknown.status_code == incorrect.status_code == 401
    assert unknown.json()["code"] == incorrect.json()["code"] == "invalid_credentials"
    assert unknown.json()["message"] == incorrect.json()["message"]


@pytest.mark.anyio
async def test_expired_session_is_rejected(auth_app) -> None:
    app, session_factory = auth_app
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/v1/auth/login",
            json={"username": "employee", "password": "employee-password"},
        )
        with session_factory() as database:
            session = database.scalar(select(Session))
            assert session is not None
            session.expires_at = utc_now() - timedelta(seconds=1)
            database.commit()
        response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"


@pytest.mark.anyio
async def test_employee_is_forbidden_from_admin_check_but_admin_is_allowed(auth_app) -> None:
    app, _session_factory = auth_app
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as employee:
        await employee.post(
            "/api/v1/auth/login",
            json={"username": "employee", "password": "employee-password"},
        )
        forbidden = await employee.get("/api/v1/auth/admin-check")

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as admin:
        await admin.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}
        )
        allowed = await admin.get("/api/v1/auth/admin-check")

    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "admin_required"
    assert allowed.status_code == 204


@pytest.mark.anyio
async def test_hr_role_round_trips_but_does_not_inherit_admin_access(auth_app) -> None:
    app, _session_factory = auth_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post(
                "/api/v1/auth/login",
                json={"username": "hr", "password": "hr-password"},
            )
        ).status_code == 204
        me = await client.get("/api/v1/auth/me")
        admin = await client.get("/api/v1/auth/admin-check")
    assert me.json() == {"username": "hr", "role": "hr"}
    assert admin.status_code == 403 and admin.json()["code"] == "admin_required"
