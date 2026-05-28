# Computer Use Agent - Session Management Backend

A scalable FastAPI backend for managing Claude-powered computer use agent sessions. Replaces the experimental Streamlit interface with production-ready session management, real-time SSE streaming, database persistence, and concurrent multi-session support.

## Overview

This project wraps the [computer-use-demo](https://github.com/anthropics/anthropic-quickstarts/tree/main/computer-use-demo) agent stack with:

- **FastAPI REST API** - Session CRUD, chat, VNC connection management
- **SSE Real-time Streaming** - Live agent progress: text, tool use, screenshots, errors
- **MySQL Persistence** - Full chat history and session state storage
- **Docker Compose** - One-command local development and remote deployment
- **VNC/noVNC** - Live browser viewport in the frontend via iframe
- **Concurrent Sessions** - Isolated browser instances per session with lock-protected state

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                  Docker Container                      │
│                                                       │
│  ┌─────────────────┐  ┌────────────────────────────┐ │
│  │  Frontend (SPA)  │  │    FastAPI Backend          │ │
│  │  index.html      │◄─┤    backend/main.py          │ │
│  │  - Session list  │  │    - REST APIs              │ │
│  │  - Chat UI       │  │    - SSE streaming          │ │
│  │  - VNC preview   │  │    - Session management     │ │
│  └─────────────────┘  └──────────┬─────────────────┘ │
│                                   │                    │
│  ┌────────────────────────────────▼─────────────────┐ │
│  │              Agent Core (browser_use_demo/)        │ │
│  │  - sampling_loop() → Claude API                   │ │
│  │  - BrowserTool → Playwright + Chromium            │ │
│  │  - ToolCollection, MessageBuilder, etc.           │ │
│  └────────────────────┬─────────────────────────────┘ │
│                       │                                │
│  ┌────────────────────▼─────────────────────────────┐ │
│  │         Virtual Display + VNC                     │ │
│  │  Xvfb → x11vnc (5900) → noVNC (6080)             │ │
│  └──────────────────────────────────────────────────┘ │
│                                                       │
│  ┌──────────────────────────────────────────────────┐ │
│  │              MySQL 8.0 (docker-compose)           │ │
│  │  - sessions table                                 │ │
│  │  - messages table                                 │ │
│  └──────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────┘
```

## Quick Start

### Prerequisites

- Docker and Docker Compose
- Anthropic API key (or PackyAPI token for Chinese candidates)

### Setup

```bash
git clone https://github.com/anthropics/anthropic-quickstarts.git
cd anthropic-quickstarts/browser-use-demo

# Configure environment
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY

# For Chinese candidates using PackyAPI, also set:
# ANTHROPIC_BASE_URL=<your-packyapi-proxy-url>
```

### Run

```bash
# Build and start all services (MySQL + app)
docker-compose up --build

# Development mode with file watching (auto-reload)
docker-compose up --build --watch
```

### Access

| Interface | URL |
|-----------|-----|
| **Frontend SPA** | http://localhost:8000 |
| **API Health Check** | http://localhost:8000/health |
| **API Docs (Swagger)** | http://localhost:8000/docs |
| **noVNC Browser View** | http://localhost:6080 |
| **Direct VNC** | localhost:5900 |

## API Reference

### Session Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/sessions` | Create a new session |
| `GET` | `/sessions` | List all active sessions |
| `GET` | `/sessions/{id}` | Get session details |
| `PATCH` | `/sessions/{id}` | Update session (title, active status) |
| `DELETE` | `/sessions/{id}` | End session and cleanup resources |

### Chat & Streaming

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/sessions/{id}/chat` | Send a message to the agent |
| `GET` | `/sessions/{id}/stream` | SSE stream for real-time agent progress |
| `GET` | `/sessions/{id}/messages` | Get message history for a session |

### VNC

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/vnc/{id}` | Get VNC/noVNC connection URLs |

### SSE Event Types

The `/sessions/{id}/stream` endpoint emits these events:

| Event | Description |
|-------|-------------|
| `text` | Text content from the agent |
| `tool_use` | Tool invocation (name, input, id) |
| `tool_result` | Tool execution result |
| `screenshot` | Base64-encoded PNG screenshot |
| `error` | Error message |
| `done` | Processing complete |
| `status` | Status update (e.g., "Thinking...") |
| `ping` | Keepalive heartbeat (every 30s) |

### Example: Create Session & Chat

```bash
# Create a session
curl -X POST http://localhost:8000/sessions \
  -H "Content-Type: application/json" \
  -d '{"title": "My Session", "model": "claude-sonnet-4-5-20250929"}'

# Send a message (returns immediately, progress via SSE)
curl -X POST http://localhost:8000/sessions/<SESSION_ID>/chat \
  -H "Content-Type: application/json" \
  -d '{"content": "Navigate to wikipedia.org and search for Claude AI"}'

# Connect to SSE stream in another terminal
curl -N http://localhost:8000/sessions/<SESSION_ID>/stream
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | (required) | Anthropic API key |
| `ANTHROPIC_BASE_URL` | (optional) | Custom API base URL (e.g., PackyAPI proxy) |
| `MYSQL_HOST` | `localhost` | MySQL hostname |
| `MYSQL_PORT` | `3306` | MySQL port |
| `MYSQL_USER` | `browseruse` | MySQL username |
| `MYSQL_PASSWORD` | `password` | MySQL password |
| `MYSQL_DB` | `browseruse` | MySQL database name |
| `VNC_PORT` | `5900` | VNC server port |
| `NOVNC_PORT` | `6080` | noVNC web proxy port |
| `API_PORT` | `8000` | FastAPI server port |

## Project Structure

```
browser-use-demo/
├── backend/                    # FastAPI backend
│   ├── main.py                 # App entry point, all API routes
│   ├── database.py             # Async SQLAlchemy + MySQL connection
│   ├── models.py               # SQLAlchemy ORM models
│   ├── schemas.py              # Pydantic request/response schemas
│   └── requirements.txt
├── browser_use_demo/           # Core agent library (reused)
│   ├── loop.py                 # sampling_loop() - Claude API interaction
│   ├── tools/
│   │   ├── browser.py          # BrowserTool - Playwright automation
│   │   ├── collection.py       # ToolCollection registry
│   │   └── coordinate_scaling.py
│   ├── message_handler.py      # Response processing & message building
│   └── browser_tool_utils/     # JavaScript utilities for DOM interaction
├── frontend/
│   └── index.html              # Single-page application frontend
├── image/                      # Docker support scripts
│   ├── entrypoint.sh           # Container startup
│   ├── start_all.sh            # Xvfb + window manager + VNC
│   └── novnc_startup.sh        # noVNC proxy
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

## Frontend

The frontend (`frontend/index.html`) is a zero-dependency single-page application that demonstrates all backend APIs:

- **Session list** with create/select/delete
- **Chat interface** with real-time streaming via EventSource (SSE)
- **Browser preview** panel using noVNC iframe
- **Settings modal** for API key, model, provider, max tokens, system prompt
- **Dark theme** with responsive layout

All settings persist in `localStorage`. No build step or npm required.

## Key Files

- **[backend/main.py](backend/main.py)** - API routes, SSE streaming, session lifecycle
- **[browser_use_demo/loop.py](browser_use_demo/loop.py)** - Core agent loop with Claude API
- **[browser_use_demo/tools/browser.py](browser_use_demo/tools/browser.py)** - Playwright-based browser automation (22 actions)
- **[frontend/index.html](frontend/index.html)** - Demo SPA frontend

## Safety Considerations

Browser automation poses unique risks distinct from standard API usage:

1. Run the browser in an isolated container environment with minimal privileges
2. Avoid exposing sensitive data or account credentials
3. Consider an allowlist of domains to reduce exposure to malicious content
4. Require human confirmation for actions with real-world consequences

This demo runs in a containerized environment. While isolated:
- **Don't enter personal credentials or sensitive information**
- **Be cautious about the websites you visit** - some sites have anti-automation measures

## Troubleshooting

**API errors?**
- Verify `ANTHROPIC_API_KEY` is set correctly in `.env`
- Check API connectivity from within the container

**Browser not visible?**
- Ensure port 6080 is accessible
- Refresh the noVNC page
- Verify Docker has sufficient resources

**Database connection refused?**
- Ensure MySQL container is healthy: `docker-compose ps`
- Check MySQL credentials in `.env` match

## Credits

Built with:
- [Anthropic Claude API](https://www.anthropic.com)
- [FastAPI](https://fastapi.tiangolo.com)
- [Playwright](https://playwright.dev)
- [NoVNC](https://novnc.com)
- [MySQL](https://www.mysql.com)
