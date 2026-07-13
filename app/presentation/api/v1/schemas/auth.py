import uuid

from pydantic import BaseModel, Field


class AppleLoginRequest(BaseModel):
    id_token: str = Field(min_length=1)


class InviteLoginRequest(BaseModel):
    invite_code: str = Field(min_length=1)
    # Client-generated persistent installation id. The invite code only grants
    # admission; identity derives from this, so one code shared across several
    # testers yields separate accounts instead of a shared wardrobe.
    device_id: str = Field(min_length=8, max_length=64)


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class AuthTokenResponse(BaseModel):
    user_id: uuid.UUID
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int
