import uuid

from pydantic import BaseModel, Field


class AppleLoginRequest(BaseModel):
    id_token: str = Field(min_length=1)


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class AuthTokenResponse(BaseModel):
    user_id: uuid.UUID
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int
