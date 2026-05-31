"""
FastAPI Backend for Computer Use Agent Session Management.

Provides:
- Session CRUD APIs with per-session Docker container isolation
- SSE (Server-Sent Events) real-time streaming
- Database persistence for chat history
- Concurrent session support with isolated desktop environments
"""

import asyncio
import base64
import json
import logging
import os
import sys
import uuid
from pathlib import Path

# Load .env file before any other imports that read environment variables
from dotenv import load_dotenv
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(env_path, override=True)
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

# Add parent directory to path so we can import browser_use_demo
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from browser_use_demo.loop import sampling_loop, APIProvider
from browser_use_demo.tools import BrowserTool, ToolResult

from .container_manager import ContainerManager, SessionContainerInfo
from .database import AsyncSessionLocal, init_db, get_session
from .models import Session as SessionModel, Message as MessageModel
from .schemas import (
    SessionCreate, SessionResponse, SessionUpdate,
    ChatRequest, MessageResponse
)
from .tasks import SessionCleanupTask

logger = logging.getLogger(__name__)


# ============================================================
# Docker container manager (session-per-container isolation)
# ============================================================

try:
    container_manager = ContainerManager()
    logger.info("ContainerManager initialized successfully.")
except RuntimeError as e:
    logger.warning("ContainerManager unavailable: %s. Sessions will use local browser fallback.", e)
    container_manager = None

cleanup_task: Optional[SessionCleanupTask] = (
    SessionCleanupTask(container_manager) if container_manager else None
)


# ============================================================
# In-memory state for active sessions
# ============================================================
active_sessions: dict[str, dict] = {}
sse_queues: dict[str, asyncio.Queue] = {}
_session_lock = asyncio.Lock()


async def _cleanup_session(session_id: str):
    """Clean up session resources including Docker container."""
    async with _session_lock:
        session_data = active_sessions.get(session_id)
        if not session_data:
            # Still clean up SSE queue even if session data is gone
            if session_id in sse_queues:
                try:
                    sse_queues[session_id].put_nowait(None)
                except Exception:
                    pass
                sse_queues.pop(session_id, None)
            return

        # 1. Clean up browser tool
        browser_tool = session_data.get("browser_tool")
        if browser_tool:
            try:
                await browser_tool.cleanup()
            except Exception as e:
                logger.warning("Error cleaning up browser tool for session %s: %s", session_id, e)

        # 2. Stop and remove session Docker container
        container_info: Optional[SessionContainerInfo] = session_data.get("container_info")
        if container_info and container_manager:
            try:
                await container_manager.stop_session_container(container_info.container_id)
                logger.info("Removed session container for %s.", session_id)
            except Exception as e:
                logger.warning("Error removing container for session %s: %s", session_id, e)

        active_sessions.pop(session_id, None)

        # 3. Close SSE queue
        if session_id in sse_queues:
            try:
                sse_queues[session_id].put_nowait(None)
            except Exception:
                pass
            sse_queues.pop(session_id, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan for startup and shutdown."""
    await init_db()

    # Start background cleanup task
    if cleanup_task:
        await cleanup_task.start()

    yield

    # Shutdown: stop cleanup task and ALL session containers
    if cleanup_task:
        await cleanup_task.stop()

    for sid in list(active_sessions.keys()):
        await _cleanup_session(sid)

    if container_manager:
        await container_manager.cleanup_all_sessions()


app = FastAPI(title="Computer Use Agent API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# Session Management APIs
# ============================================================

@app.post("/sessions", response_model=SessionResponse, status_code=201)
async def create_session(
    session_data: SessionCreate,
    db: AsyncSession = Depends(get_session),
):
    """Create a new session with an isolated Docker container."""
    session_id = str(uuid.uuid4())
    now = datetime.utcnow()

    # --- Create per-session Docker container ---
    container_info: Optional[SessionContainerInfo] = None
    if container_manager:
        try:
            container_info = await container_manager.create_session_container(session_id)
        except Exception as e:
            logger.exception("Failed to create session container.")
            raise HTTPException(
                status_code=503,
                detail=f"Failed to create session container: {e}",
            )
    else:
        logger.warning(
            "No ContainerManager available — session %s will use local browser (no isolation).",
            session_id,
        )

    # --- Persist to database ---
    db_session = SessionModel(
        id=session_id,
        title=session_data.title or f"Session {now.strftime('%Y-%m-%d %H:%M')}",
        created_at=now,
        updated_at=now,
        is_active=True,
        container_id=container_info.container_id if container_info else None,
        container_name=container_info.container_name if container_info else None,
        novnc_port=container_info.novnc_host_port if container_info else None,
        cdp_url=container_info.cdp_url if container_info else None,
    )
    db.add(db_session)
    await db.commit()
    await db.refresh(db_session)

    # --- Create BrowserTool (CDP mode if container, local otherwise) ---
    cdp_url = container_info.cdp_url if container_info else None
    browser_tool = BrowserTool(cdp_url=cdp_url)

    async with _session_lock:
        active_sessions[session_id] = {
            "browser_tool": browser_tool,
            "container_info": container_info,
            "messages": [],
            "model": session_data.model,
            "provider": session_data.provider,
            "system_prompt": session_data.system_prompt,
            "max_tokens": session_data.max_tokens,
            "_saved_count": 0,
            "_processing": False,
        }
        sse_queues[session_id] = asyncio.Queue()

    return SessionResponse(
        id=db_session.id,
        title=db_session.title,
        created_at=db_session.created_at,
        updated_at=db_session.updated_at,
        is_active=db_session.is_active,
        model=session_data.model,
        provider=session_data.provider,
        novnc_port=container_info.novnc_host_port if container_info else None,
        container_status="running" if container_info else None,
    )


@app.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(db: AsyncSession = Depends(get_session)):
    """List all active sessions."""
    result = await db.execute(
        select(SessionModel)
        .where(SessionModel.is_active == True)
        .order_by(SessionModel.updated_at.desc())
    )
    sessions = result.scalars().all()

    # Enrich with live container status
    output = []
    for s in sessions:
        container_status = None
        if s.container_id and container_manager:
            try:
                container_status = await container_manager.get_container_status(s.container_id)
            except Exception:
                container_status = "unknown"
        output.append(
            SessionResponse(
                id=s.id,
                title=s.title,
                created_at=s.created_at,
                updated_at=s.updated_at,
                is_active=s.is_active,
                novnc_port=s.novnc_port,
                container_status=container_status,
            )
        )
    return output


@app.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session_by_id(
    session_id: str,
    db: AsyncSession = Depends(get_session),
):
    """Get a session by ID."""
    result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    container_status = None
    if session.container_id and container_manager:
        try:
            container_status = await container_manager.get_container_status(session.container_id)
        except Exception:
            container_status = "unknown"

    return SessionResponse(
        id=session.id,
        title=session.title,
        created_at=session.created_at,
        updated_at=session.updated_at,
        is_active=session.is_active,
        novnc_port=session.novnc_port,
        container_status=container_status,
    )


@app.patch("/sessions/{session_id}", response_model=SessionResponse)
async def update_session(
    session_id: str,
    update_data: SessionUpdate,
    db: AsyncSession = Depends(get_session),
):
    """Update a session."""
    result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if update_data.title is not None:
        session.title = update_data.title
    if update_data.is_active is not None:
        session.is_active = update_data.is_active
    session.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(session)
    return SessionResponse(
        id=session.id,
        title=session.title,
        created_at=session.created_at,
        updated_at=session.updated_at,
        is_active=session.is_active,
        novnc_port=session.novnc_port,
    )


@app.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_session),
):
    """Deactivate a session and clean up its Docker container."""
    result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    session.is_active = False
    session.container_id = None
    session.container_name = None
    session.novnc_port = None
    session.cdp_url = None
    session.updated_at = datetime.utcnow()
    await db.commit()
    await _cleanup_session(session_id)


@app.get("/sessions/{session_id}/messages", response_model=list[MessageResponse])
async def get_messages(
    session_id: str,
    db: AsyncSession = Depends(get_session),
):
    """Get all messages for a session."""
    result = await db.execute(
        select(MessageModel)
        .where(MessageModel.session_id == session_id)
        .order_by(MessageModel.created_at.asc(), MessageModel.id.asc())
    )
    messages = result.scalars().all()
    return [
        MessageResponse(
            id=m.id,
            session_id=m.session_id,
            role=m.role,
            content=m.content,
            created_at=m.created_at,
        )
        for m in messages
    ]


@app.get("/vnc/{session_id}")
async def get_vnc_info(session_id: str, request: Request):
    """Get VNC connection info for a session. Returns session-specific noVNC port."""

    novnc_port: Optional[int] = None

    # 1. Check in-memory active sessions first
    if session_id in active_sessions:
        container_info = active_sessions[session_id].get("container_info")
        if container_info:
            novnc_port = container_info.novnc_host_port

    # 2. Fallback: load from database
    if novnc_port is None:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SessionModel).where(SessionModel.id == session_id)
            )
            session = result.scalar_one_or_none()
            if session and session.novnc_port:
                novnc_port = session.novnc_port

    if novnc_port is None:
        raise HTTPException(
            status_code=404,
            detail="Session not active or no VNC available. Create a session first.",
        )

    host = request.headers.get("host", "localhost").split(":")[0]
    return {
        "session_id": session_id,
        "novnc_port": novnc_port,
        "novnc_url": f"http://{host}:{novnc_port}/vnc.html?host={host}&port={novnc_port}",
    }


# ============================================================
# SSE Helper
# ============================================================

def _sse_event(event_type: str, data: dict) -> str:
    """Format a dict as an SSE event string."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


async def _send_sse(session_id: str, event_type: str, data: dict):
    """Push an event to the SSE queue for a session."""
    if session_id in sse_queues:
        try:
            await sse_queues[session_id].put(json.dumps({"event": event_type, "data": data}))
        except Exception:
            pass


# ============================================================
# SSE Streaming & Chat Endpoints
# ============================================================

@app.get("/sessions/{session_id}/stream")
async def sse_stream(session_id: str, request: Request):
    """
    SSE endpoint for real-time streaming from the agent.
    Client connects via EventSource to receive events: text, tool_use, tool_result, screenshot, error, done, status.
    """
    if session_id not in active_sessions:
        async def error_gen():
            yield _sse_event("error", {"content": "Session not found. Create a session first."})
        return StreamingResponse(error_gen(), media_type="text/event-stream")

    queue: asyncio.Queue = sse_queues[session_id]

    async def event_generator():
        # Send initial connected event so the client knows the stream is live
        yield _sse_event("connected", {"session_id": session_id})
        while True:
            if await request.is_disconnected():
                break
            try:
                msg_str = await asyncio.wait_for(queue.get(), timeout=30.0)
                if msg_str is None:
                    yield _sse_event("done", {"message": "Session ended"})
                    break
                msg = json.loads(msg_str)
                yield _sse_event(msg["event"], msg["data"])
            except asyncio.TimeoutError:
                yield _sse_event("ping", {"timestamp": datetime.utcnow().isoformat()})
            except Exception:
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


@app.post("/sessions/{session_id}/chat")
async def chat_endpoint(
    session_id: str,
    chat_request: ChatRequest,
):
    """Send a message to the agent and trigger processing."""
    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Session not active")

    session_data = active_sessions[session_id]

    if session_data.get("_processing", False):
        raise HTTPException(status_code=409, detail="Agent is still processing the previous request")

    session_data["_processing"] = True
    browser_tool = session_data["browser_tool"]

    user_content = chat_request.content
    user_message = {
        "role": "user",
        "content": [{"type": "text", "text": user_content}],
    }
    session_data["messages"].append(user_message)

    await _save_message(session_id, "user", json.dumps(user_message["content"]))
    session_data["_saved_count"] = len(session_data["messages"])
    await _send_sse(session_id, "status", {"content": "Thinking..."})

    async def output_callback(content_block):
        if content_block.get("type") == "text":
            await _send_sse(session_id, "text", {"content": content_block.get("text", "")})
        elif content_block.get("type") == "tool_use":
            await _send_sse(session_id, "tool_use", {
                "name": content_block.get("name"),
                "input": content_block.get("input"),
                "id": content_block.get("id"),
            })

    async def tool_output_callback(result: ToolResult, tool_id: str):
        tool_result_data = {
            "tool_id": tool_id,
            "output": (result.output[:500] if result.output else None),
            "error": result.error,
        }
        if result.base64_image:
            await _send_sse(session_id, "screenshot", {"base64": result.base64_image})
        await _send_sse(session_id, "tool_result", tool_result_data)

    async def api_response_callback(request, response, error):
        if error:
            await _send_sse(session_id, "error", {"content": str(error)})

    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        provider = APIProvider(session_data.get("provider", "anthropic"))

        updated_messages = await sampling_loop(
            model=session_data.get("model", "claude-sonnet-4-6"),
            provider=provider,
            system_prompt_suffix=session_data.get("system_prompt", ""),
            messages=session_data["messages"],
            output_callback=output_callback,
            tool_output_callback=tool_output_callback,
            api_response_callback=api_response_callback,
            api_key=api_key,
            max_tokens=session_data.get("max_tokens", 8192),
            browser_tool=browser_tool,
        )

        session_data["messages"] = updated_messages

        saved_count = session_data.get("_saved_count", 0)
        screenshots_dir = os.path.join(os.path.dirname(__file__), "..", "image")
        for msg in updated_messages[saved_count:]:
            role = msg.get("role", "assistant")
            content_str = json.dumps(msg.get("content", []))
            content_str = _save_screenshots_from_content(content_str, session_id, screenshots_dir)
            await _save_message(session_id, role, content_str)
        session_data["_saved_count"] = len(updated_messages)

        # Update session's updated_at timestamp
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SessionModel).where(SessionModel.id == session_id)
            )
            db_session = result.scalar_one_or_none()
            if db_session:
                db_session.updated_at = datetime.utcnow()
                await db.commit()

    except Exception as e:
        await _send_sse(session_id, "error", {"content": str(e)})
    finally:
        session_data["_processing"] = False
        await _send_sse(session_id, "done", {"message": "Processing complete"})


def _save_screenshots_from_content(content_str: str, session_id: str, screenshots_dir: str) -> str:
    """Extract base64 images from message content, save to disk, replace with file refs.

    Walks all content blocks (top-level and nested inside tool_result.content arrays),
    saves each base64 image as a PNG file, and replaces the base64 data with a file
    reference so the database stores lightweight JSON.

    Returns the modified JSON string, or the original string if no images were found.
    """
    try:
        blocks = json.loads(content_str)
    except (json.JSONDecodeError, TypeError):
        return content_str

    if not isinstance(blocks, list):
        return content_str

    def _process_block(block: dict) -> dict | None:
        """Process a single block – return replacement or None to keep original."""
        if not isinstance(block, dict):
            return None
        if block.get("type") == "image":
            source = block.get("source", {})
            if isinstance(source, dict) and source.get("type") == "base64":
                b64_data = source.get("data", "")
                if b64_data:
                    try:
                        img_bytes = base64.b64decode(b64_data)
                    except Exception:
                        logger.warning("Failed to decode base64 image for session %s", session_id)
                        return None
                    # Ensure directory exists
                    img_dir = os.path.join(screenshots_dir, "screenshots", session_id)
                    os.makedirs(img_dir, exist_ok=True)
                    filename = f"{uuid.uuid4().hex}.png"
                    filepath = os.path.join(img_dir, filename)
                    try:
                        with open(filepath, "wb") as f:
                            f.write(img_bytes)
                    except Exception as e:
                        logger.warning("Failed to save screenshot for session %s: %s", session_id, e)
                        return None
                    relative_path = f"screenshots/{session_id}/{filename}"
                    logger.info("Saved screenshot: %s (%d bytes)", relative_path, len(img_bytes))
                    return {"type": "image_file", "path": relative_path}
        # Recurse into tool_result blocks — also truncate long text in tool results
        if block.get("type") == "tool_result":
            inner = block.get("content")
            if isinstance(inner, list):
                new_inner = _process_content_list(inner, inside_tool_result=True)
                return {**block, "content": new_inner}
        return None

    def _process_content_list(content_list: list, inside_tool_result: bool = False) -> list:
        """Process a list of content blocks, replacing image blocks with file refs
        and truncating long text inside tool_result entries."""
        result = []
        for block in content_list:
            replacement = _process_block(block)
            if replacement is not None:
                result.append(replacement)
            else:
                # Truncate massive text blocks inside tool_result (e.g. get_page_text output)
                if inside_tool_result and isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if isinstance(text, str) and len(text) > 2000:
                        truncated = text[:2000] + f"\n\n... [truncated {len(text) - 2000} chars for storage]"
                        result.append({**block, "text": truncated})
                        logger.info("Truncated tool_result text: %d → %d chars", len(text), len(truncated))
                        continue
                result.append(block)
        return result

    new_blocks = _process_content_list(blocks)
    return json.dumps(new_blocks)


async def _save_message(session_id: str, role: str, content: str):
    """Save a message to the database."""
    try:
        async with AsyncSessionLocal() as db:
            msg = MessageModel(
                session_id=session_id,
                role=role,
                content=content,
            )
            db.add(msg)
            await db.commit()
    except Exception as e:
        logger.error("Failed to save message for session %s: %s", session_id, e)


# ============================================================
# Health Check
# ============================================================

@app.get("/health")
async def health_check():
    """Health check endpoint with container stats."""
    running_containers = 0
    if container_manager:
        try:
            containers = await container_manager.list_session_containers()
            running_containers = len(containers)
        except Exception:
            pass

    return {
        "status": "healthy",
        "active_sessions": len(active_sessions),
        "running_containers": running_containers,
        "container_manager_available": container_manager is not None,
        "timestamp": datetime.utcnow().isoformat(),
    }


# ============================================================
# Mount static screenshot files (before frontend so /screenshots/ routes take priority)
# ============================================================

screenshots_dir = os.path.join(os.path.dirname(__file__), "..", "image")
# Ensure the directory exists (including first run and Docker environments)
os.makedirs(os.path.join(screenshots_dir, "screenshots"), exist_ok=True)
app.mount("/screenshots", StaticFiles(directory=screenshots_dir), name="screenshots")

# ============================================================
# Mount static frontend files
# ============================================================

frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")


# ============================================================
# Entry point for running directly
# ============================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=int(os.getenv("API_PORT", "8000")),
        reload=True,
    )
