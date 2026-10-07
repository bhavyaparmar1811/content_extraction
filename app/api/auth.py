"""Authentication endpoints — Secret questions, Signup, Login, Refresh, Me, and Logout."""

import hashlib
import uuid
from typing import Optional

from fastapi import APIRouter, Request, Response, Header, Depends, status
from fastapi.responses import JSONResponse

from app.config.settings import Settings, get_settings
from app.core.exceptions import AppError
from app.schemas.auth import (
    SecretQuestionsResponse,
    SecretQuestionsData,
    SecretQuestionItem,
    SignupRequest,
    SignupSuccessResponse,
    LoginRequest,
    LoginSuccessResponse,
    CurrentUserResponse,
    CurrentUserData,
    ApiMeta,
)
from app.stores.auth_store import AuthStore
from app.services.auth import (
    SignupService,
    LoginService,
    LoginError,
    TokenService,
)

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])


def get_auth_store(request: Request, settings: Settings = Depends(get_settings)) -> AuthStore:
    """Retrieve or create AuthStore instance from application state."""
    auth_store = getattr(request.app.state, "auth_store", None)
    if auth_store is None:
        db_path = settings.auth_db_path or (settings.project_root / "data" / "auth.db")
        auth_store = AuthStore(db_path, settings)
        request.app.state.auth_store = auth_store
    return auth_store


def _correlation_id(request: Request, header_value: Optional[str]) -> str:
    return header_value or getattr(request.state, "correlation_id", None) or str(uuid.uuid4())


@router.get(
    "/secret-questions",
    response_model=SecretQuestionsResponse,
    summary="Retrieve active secret questions",
)
async def get_secret_questions(
    request: Request,
    x_correlation_id: Optional[str] = Header(None, alias="X-Correlation-ID"),
    auth_store: AuthStore = Depends(get_auth_store),
):
    """Return the list of active secret questions for signup."""
    correlation_id = _correlation_id(request, x_correlation_id)
    try:
        raw_items = auth_store.get_active_secret_questions()
        items = [SecretQuestionItem(**item) for item in raw_items]
        return SecretQuestionsResponse(
            success=True,
            data=SecretQuestionsData(items=items),
            meta=ApiMeta(correlationId=correlation_id),
        )
    except AppError:
        raise
    except Exception:
        raise AppError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="SECRET_QUESTIONS_UNAVAILABLE",
            message="Secret questions are temporarily unavailable. Please try again later.",
        )


@router.post(
    "/signup",
    response_model=SignupSuccessResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user account",
)
async def signup(
    body: SignupRequest,
    request: Request,
    x_correlation_id: Optional[str] = Header(None, alias="X-Correlation-ID"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    settings: Settings = Depends(get_settings),
    auth_store: AuthStore = Depends(get_auth_store),
):
    """Create a new user account with default REGULAR_USER role and issue initial JWT."""
    correlation_id = _correlation_id(request, x_correlation_id)
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    signup_service = SignupService(settings, auth_store)
    return signup_service.validate_and_register(
        request=body,
        correlation_id=correlation_id,
        ip_address=client_ip,
        user_agent=user_agent,
    )


@router.post(
    "/login",
    response_model=LoginSuccessResponse,
    status_code=status.HTTP_200_OK,
    summary="Authenticate credentials and establish a session",
)
async def login(
    body: LoginRequest,
    request: Request,
    x_correlation_id: Optional[str] = Header(None, alias="X-Correlation-ID"),
    settings: Settings = Depends(get_settings),
    auth_store: AuthStore = Depends(get_auth_store),
):
    """Authenticate BI email and password, returning short-lived JWT and setting HttpOnly refresh cookie."""
    correlation_id = _correlation_id(request, x_correlation_id)
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    login_service = LoginService(settings, auth_store)
    auth_result = login_service.authenticate(
        request=body,
        correlation_id=correlation_id,
        ip_address=client_ip,
        user_agent=user_agent,
    )

    response_data = auth_result["response"].model_dump()
    response = JSONResponse(
        status_code=status.HTTP_200_OK,
        content=response_data,
    )
    response.set_cookie(
        key="refresh_token",
        value=auth_result["raw_refresh_token"],
        max_age=auth_result["cookie_max_age"],
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/api/v1/auth",
    )
    return response


@router.post(
    "/refresh",
    response_model=LoginSuccessResponse,
    status_code=status.HTTP_200_OK,
    summary="Rotate refresh token and issue new access token",
)
async def refresh_token(
    request: Request,
    x_correlation_id: Optional[str] = Header(None, alias="X-Correlation-ID"),
    settings: Settings = Depends(get_settings),
    auth_store: AuthStore = Depends(get_auth_store),
):
    """Read HttpOnly refresh cookie, rotate the session, and return a new access token."""
    correlation_id = _correlation_id(request, x_correlation_id)
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    raw_token = request.cookies.get("refresh_token")
    login_service = LoginService(settings, auth_store)
    refresh_result = login_service.refresh_session(
        raw_refresh_token=raw_token,
        correlation_id=correlation_id,
        ip_address=client_ip,
        user_agent=user_agent,
    )

    response_data = refresh_result["response"].model_dump()
    response = JSONResponse(
        status_code=status.HTTP_200_OK,
        content=response_data,
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_result["raw_refresh_token"],
        max_age=refresh_result["cookie_max_age"],
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/api/v1/auth",
    )
    return response


async def require_user(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    settings: Settings = Depends(get_settings),
    auth_store: AuthStore = Depends(get_auth_store),
) -> dict:
    """Dependency: the active user behind the Bearer access token, or 401/403."""
    if not authorization or not authorization.startswith("Bearer "):
        raise LoginError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="UNAUTHENTICATED",
            message="Missing or invalid Authorization header.",
        )

    token = authorization.split(" ", 1)[1].strip()
    token_service = TokenService(settings)

    try:
        decoded = token_service.decode_access_token(token)
    except Exception:
        raise LoginError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="INVALID_TOKEN",
            message="Access token is invalid or expired.",
        )

    user_id = decoded.get("sub")
    user = auth_store.get_user_by_id(user_id) if user_id else None

    if not user:
        raise LoginError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="USER_NOT_FOUND",
            message="User associated with token does not exist.",
        )

    if user["status"] == "DISABLED":
        raise LoginError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="ACCOUNT_DISABLED",
            message="User account is disabled.",
        )

    if not user["roleIsActive"]:
        raise LoginError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="ROLE_INACTIVE",
            message="User role is inactive.",
        )
    return user


@router.get(
    "/me",
    response_model=CurrentUserResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve authenticated user profile",
)
async def get_current_user(
    request: Request,
    x_correlation_id: Optional[str] = Header(None, alias="X-Correlation-ID"),
    user: dict = Depends(require_user),
):
    """Validate Bearer access token and return user profile."""
    correlation_id = _correlation_id(request, x_correlation_id)

    return CurrentUserResponse(
        success=True,
        data=CurrentUserData(
            userId=user["userId"],
            firstName=user["firstName"],
            lastName=user["lastName"],
            biEmail=user["biEmail"],
            role=user["role"],
            status=user["status"],
            emailVerified=bool(user["emailVerified"]),
        ),
        meta=ApiMeta(correlationId=correlation_id),
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke session and clear refresh cookie",
)
async def logout(
    request: Request,
    x_correlation_id: Optional[str] = Header(None, alias="X-Correlation-ID"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
    settings: Settings = Depends(get_settings),
    auth_store: AuthStore = Depends(get_auth_store),
):
    """Revoke current authenticated session and delete HttpOnly refresh cookie."""
    correlation_id = _correlation_id(request, x_correlation_id)
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    session_id: Optional[str] = None
    user_id: Optional[str] = None

    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1].strip()
        try:
            token_service = TokenService(settings)
            decoded = token_service.decode_access_token(token)
            session_id = decoded.get("sid")
            user_id = decoded.get("sub")
        except Exception:
            pass

    if not session_id:
        raw_cookie = request.cookies.get("refresh_token")
        if raw_cookie:
            token_hash = hashlib.sha256(raw_cookie.encode("utf-8")).hexdigest()
            session = auth_store.get_session_by_token_hash(token_hash)
            if session:
                session_id = session["sessionId"]
                user_id = session["userId"]

    login_service = LoginService(settings, auth_store)
    login_service.logout(
        session_id=session_id,
        user_id=user_id,
        correlation_id=correlation_id,
        ip_address=client_ip,
        user_agent=user_agent,
    )

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        key="refresh_token",
        path="/api/v1/auth",
        httponly=True,
        samesite="lax",
    )
    return response
