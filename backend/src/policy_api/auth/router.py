from __future__ import annotations

from collections.abc import Generator
import secrets

from fastapi import APIRouter, Cookie, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session as DatabaseSession

from policy_api.auth.schemas import CurrentUserResponse, LoginRequest
from policy_api.auth.service import authenticate, issue_session, resolve_user, revoke_session
from policy_api.models import Session, User, UserRole
from policy_api.rate_limit import client_key, limited_response


COOKIE_NAME = "policy_session"
CSRF_COOKIE_NAME = "policy_csrf"
router = APIRouter(prefix="/auth", tags=["authentication"])


def database_session(request: Request) -> Generator[DatabaseSession, None, None]:
    with request.app.state.session_factory() as database:
        yield database


def error(request: Request, status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code, "message": message, "request_id": request.state.request_id})


def current_identity(
    request: Request,
    policy_session: str | None = Cookie(default=None),
    database: DatabaseSession = Depends(database_session),
) -> tuple[User, Session]:
    identity = resolve_user(database, policy_session)
    if identity is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="authentication_required")
    return identity


@router.post("/login", response_model=None)
def login(payload: LoginRequest, request: Request, database: DatabaseSession = Depends(database_session)) -> Response:
    retry_after = request.app.state.rate_limiter.check(
        f"login:{client_key(request)}", request.app.state.settings.login_rate_limit_per_minute)
    if retry_after is not None: return limited_response(request, retry_after)
    user = authenticate(database, payload.username, payload.password)
    if user is None:
        return error(request, 401, "invalid_credentials", "The username or password is incorrect.")
    token = issue_session(database, user)
    response = Response(status_code=204)
    response.set_cookie(COOKIE_NAME, token.raw_token, httponly=True, samesite="lax", secure=request.app.state.settings.app_env == "production", max_age=8 * 60 * 60)
    response.set_cookie(CSRF_COOKIE_NAME, secrets.token_urlsafe(32), httponly=False, samesite="lax",
        secure=request.app.state.settings.app_env == "production", max_age=8 * 60 * 60)
    return response


@router.get("/me", response_model=CurrentUserResponse)
def me(identity: tuple[User, Session] = Depends(current_identity)) -> CurrentUserResponse:
    user, _session = identity
    return CurrentUserResponse(username=user.username, role=user.role.value)


@router.post("/logout", response_model=None)
def logout(identity: tuple[User, Session] = Depends(current_identity), database: DatabaseSession = Depends(database_session)) -> Response:
    _user, session = identity
    revoke_session(database, session)
    response = Response(status_code=204)
    response.delete_cookie(COOKIE_NAME, httponly=True, samesite="lax")
    response.delete_cookie(CSRF_COOKIE_NAME, httponly=False, samesite="lax")
    return response


@router.get("/admin-check", response_model=None)
def admin_check(request: Request, identity: tuple[User, Session] = Depends(current_identity)) -> Response:
    user, _session = identity
    if user.role != UserRole.ADMIN:
        return error(request, 403, "admin_required", "Administrator access is required.")
    return Response(status_code=204)
