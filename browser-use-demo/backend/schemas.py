from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class SessionCreate(BaseModel):
    title: Optional[str] = None
    model: str = Field(default="claude-sonnet-4-6", description="Claude model to use")
    provider: str = Field(default="anthropic", description="API provider: anthropic, bedrock, vertex")
    system_prompt: str = Field(default="", description="Additional system prompt instructions")
    max_tokens: int = Field(default=8192, ge=1, le=32000)
    display_width: int = Field(default=1920, description="Session display width in pixels")
    display_height: int = Field(default=1080, description="Session display height in pixels")


class SessionResponse(BaseModel):
    id: str
    title: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    is_active: bool
    model: str = "claude-sonnet-4-5-20250929"
    provider: str = "anthropic"
    novnc_port: Optional[int] = None
    container_status: Optional[str] = None

    class Config:
        from_attributes = True


class SessionUpdate(BaseModel):
    title: Optional[str] = None
    is_active: Optional[bool] = None


class ChatRequest(BaseModel):
    content: str = Field(..., description="User message content")


class MessageResponse(BaseModel):
    id: int
    session_id: str
    role: str
    content: str
    created_at: datetime

    class Config:
        from_attributes = True


class WSMessage(BaseModel):
    type: str  # chat, ping
    content: Optional[str] = None


class WSOutgoingMessage(BaseModel):
    type: str  # text, tool_use, tool_result, screenshot, error, done, status
    content: Optional[str] = None
    data: Optional[dict] = None