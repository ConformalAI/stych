"""
User and authentication models (Pydantic schemas for API request/response).
"""

from typing import Optional

from pydantic import BaseModel, Field


class UserBase(BaseModel):
    """Base user model."""

    email: str
    name: Optional[str] = None


class UserResponse(BaseModel):
    """User response model (public fields)."""

    id: str
    email: str
    handle: Optional[str] = None
    name: Optional[str] = None
    profile_picture: Optional[str] = None
    is_active: bool
    can_change_password: bool = False
    created_at: Optional[str] = None


class GoogleAuthRequest(BaseModel):
    """Request body for Google OAuth authentication."""

    credential: str  # Google ID token (JWT) from Google Sign-In


class AuthResponse(BaseModel):
    """Response after successful authentication."""

    message: str
    user: UserResponse


class UpdateUserRequest(BaseModel):
    """Editable user profile fields."""

    handle: Optional[str] = Field(default=None, max_length=50)
    name: Optional[str] = Field(default=None, max_length=120)
    profile_picture: Optional[str] = Field(default=None, max_length=2048)


class ChangePasswordRequest(BaseModel):
    """Payload for authenticated password change."""

    current_password: str = Field(min_length=8, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class UsageByModelResponse(BaseModel):
    model: str
    request_count: int
    total_tokens: int
    estimated_cost_usd: float
    credits_consumed: float


class UsageEventResponse(BaseModel):
    id: str
    created_at: Optional[str] = None
    provider: str
    model: str
    agent_name: Optional[str] = None
    session_id: Optional[str] = None
    latency_ms: Optional[float] = None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    credits_consumed: float


class UsageSummaryResponse(BaseModel):
    window_days: int
    request_count: int
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float
    credits_consumed: float
    by_model: list[UsageByModelResponse]
    recent_events: list[UsageEventResponse]


class UsageSessionSummaryResponse(BaseModel):
    session_id: str
    request_count: int
    total_tokens: int
    estimated_cost_usd: float
    credits_consumed: float
    first_event_at: Optional[str] = None
    last_event_at: Optional[str] = None
    is_anonymous: bool = False
    title: Optional[str] = None


class UsageSessionListResponse(BaseModel):
    window_days: int
    sessions: list[UsageSessionSummaryResponse]


class SessionSummaryResponse(BaseModel):
    session_id: str
    title: Optional[str] = None
    created_at: Optional[str] = None
    last_activity_at: Optional[str] = None
    request_count: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    credits_consumed: float = 0.0
    is_anonymous: bool = False


class SessionListResponse(BaseModel):
    window_days: Optional[int] = None
    sessions: list[SessionSummaryResponse]
    has_more: bool = False
    next_before: Optional[str] = None
    next_before_id: Optional[str] = None


class UsageEventsPageResponse(BaseModel):
    window_days: int
    page: int
    page_size: int
    total_count: int
    items: list[UsageEventResponse]


class Token(BaseModel):
    """Access token model."""

    access_token: str
    token_type: str = "bearer"


class TokenPayload(BaseModel):
    """Token payload model."""

    user_id: str
    email: str
    name: Optional[str] = None
    exp: Optional[int] = None
