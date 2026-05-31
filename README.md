# Computer Use Agent — Browser Automation with Claude

A containerized browser automation platform powered by Claude. Each session runs in its own isolated Docker container with a full desktop environment (Xvfb + VNC + Chromium), streamed to your browser in real time via SSE and noVNC.

## Features

- **Session-per-Container Isolation** — Every session gets its own Docker container with an independent desktop, browser, and VNC stream
- **Real-time SSE Streaming** — Live agent progress: text, tool calls, screenshots, and results pushed to the frontend
- **Built-in VNC Viewer** — Watch the agent control the desktop in real time via noVNC (dynamically allocated per session)
- **MySQL Persistence** — Full chat history with screenshots preserved across restarts
- **Tool Calling** — Agent actually controls the browser (navigate, click, type, scroll, execute JS, take screenshots, etc.)
- **Zero-Build Frontend** — Single HTML file SPA, no npm/webpack required
- **Docker Compose** — One command to start MySQL + API + frontend
- **Proxy Support** — Works with Anthropic-native proxy services (PackyAPI) for users in China

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    Docker Host                                │
│                                                              │
│  ┌─ browser-use-main ──────────────────────────────────────┐│
│  │  FastAPI (uvicorn)                                       ││
│  │  ├─ /sessions          REST CRUD                        ││
│  │  ├─ /sessions/{id}/chat      Chat endpoint              ││
│  │  ├─ /sessions/{id}/stream    SSE streaming              ││
│  │  ├─ /vnc/{id}                VNC connection info        ││
│  │  └─ /                        Static frontend SPA        ││
│  └──────────────────────────────────────────────────────────┘│
│           │                                                  │
│           │ Docker SDK (ContainerManager)                    │
│           ▼                                                  │
│  ┌─ session-<id-1> ───┐  ┌─ session-<id-2> ───┐             │
│  │ Xvfb + mutter      │  │ Xvfb + mutter      │             │
│  │ Chromium (CDP)     │  │ Chromium (CDP)     │             │
│  │ x11vnc + noVNC     │  │ x11vnc + noVNC     │             │
│  │ :6081              │  │ :6082              │             │
│  └────────────────────┘  └────────────────────┘             │
│                                                              │
│  ┌─ browser-use-mysql ─────────────────────────────────────┐│
│  │  MySQL 8.0                                              ││
│  │  ├─ sessions  (session metadata)                        ││
│  │  └─ messages  (chat history, screenshots)               ││
│  └──────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────┘
```

**How it works:**

1. User creates a **session** → backend spawns an isolated Docker container with a full Linux desktop
2. User sends a **chat message** → Claude (via Anthropic API or proxy) decides which browser action to take
3. Agent calls **tools** (navigate, click, type, screenshot, etc.) → Playwright executes them in the session's Chromium
4. **Real-time feedback** flows back via SSE: tool calls, screenshots, text responses
5. Every action is visible in the **noVNC pane** — watch the desktop in real time

## Quick Start

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- Anthropic API key (get one at [console.anthropic.com](https://console.anthropic.com))
- For users in China: a proxy service like PackyAPI that supports the native Anthropic API format

### 1. Clone & Configure

```bash
git clone https://github.com/xueyu951229-dev/borwser-user-zh.git
cd browser-use-demo

# Create your .env file
cp .env.example .env
```

Edit `.env` and add your API key:

```env
ANTHROPIC_API_KEY=sk-your-api-key-here

# For users in China using a proxy:
ANTHROPIC_BASE_URL=https://your-proxy-url.com
```

### 2. Build & Start

```bash
# Build the Docker image (first time only, ~5 minutes)
docker compose build

# Start all services in the background
docker compose up -d
```

### 3. Access

| Interface | URL |
|-----------|-----|
| **Frontend** | http://localhost:8000 |
| **API Docs (Swagger)** | http://localhost:8000/docs |
| **Health Check** | http://localhost:8000/health |

### 4. Create a Session

Open http://localhost:8000 and click **"+ New Session"**. This creates an isolated browser container. The session's noVNC port will be shown (e.g., 6081).

### 5. Start Controlling the Browser

Type a command like:

> "Navigate to example.com and tell me the page title"

The agent will:
1. Open the browser
2. Navigate to the URL
3. Take a screenshot
4. Read the page
5. Stream everything back in real time

Click **"🔗 Open VNC"** to watch the desktop live.

## Using the Frontend

The SPA has three panels:

| Panel | Purpose |
|-------|---------|
| **Left sidebar** | Session list — create, select, delete sessions |
| **Center** | Chat interface — send messages, view agent responses and screenshots |
| **Right** | Browser preview — noVNC iframe showing the live desktop |

**Settings** (⚙️ icon):
- API key (stored in localStorage, not sent to server)
- Model selection (Claude Sonnet 4.6, Opus 4.7, etc.)
- Max tokens
- Custom system prompt

**Keyboard**: Press `Enter` to send, `Ctrl+Enter` for newline.

## API Overview

### Session Management

```bash
# Create a session
curl -X POST http://localhost:8000/sessions \
  -H "Content-Type: application/json" \
  -d '{"title": "My Session", "model": "claude-sonnet-4-6", "provider": "anthropic"}'

# List sessions
curl http://localhost:8000/sessions

# Get session details (includes noVNC port)
curl http://localhost:8000/sessions/<SESSION_ID>
```

### Chat & Streaming

Send a message and receive real-time events. Connect to SSE **first**, then send chat:

```bash
# Terminal 1: Connect to SSE stream
curl -N http://localhost:8000/sessions/<SESSION_ID>/stream

# Terminal 2: Send a message
curl -X POST http://localhost:8000/sessions/<SESSION_ID>/chat \
  -H "Content-Type: application/json" \
  -d '{"content": "Navigate to wikipedia.org"}'
```

### SSE Events

| Event | Description |
|-------|-------------|
| `connected` | Stream established (session ID) |
| `status` | Status update ("Thinking...") |
| `text` | Agent text response (streamed token by token) |
| `tool_use` | Tool invocation (name, input, id) |
| `tool_result` | Tool execution result (output/error) |
| `screenshot` | Base64-encoded JPEG screenshot |
| `error` | Error message |
| `done` | Current turn complete (stream stays open for multi-turn) |
| `ping` | Keepalive heartbeat (every 30s) |

### VNC

```bash
# Get VNC connection info
curl http://localhost:8000/vnc/<SESSION_ID>
# Returns: {"session_id": "...", "novnc_port": 6081, "novnc_url": "http://..."}
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | *(required)* | Anthropic API key |
| `ANTHROPIC_BASE_URL` | *(empty)* | Proxy base URL (e.g., PackyAPI). Uses native Anthropic protocol for tool calling support |
| `API_PORT` | `8000` | FastAPI server port |
| `MYSQL_HOST` | `mysql` | MySQL hostname |
| `MYSQL_PORT` | `3306` | MySQL port |
| `MYSQL_USER` | `browseruse` | MySQL user |
| `MYSQL_PASSWORD` | `password` | MySQL password |
| `MYSQL_DB` | `browseruse` | Database name |
| `MYSQL_ROOT_PASSWORD` | `rootpassword` | MySQL root password |
| `NOVNC_PORT_MIN` | `6081` | Start of per-session noVNC port range |
| `NOVNC_PORT_MAX` | `6180` | End of per-session noVNC port range |
| `SESSION_IMAGE` | `browser-use-demo:latest` | Docker image for session containers |
| `SESSION_MEM_LIMIT` | `2g` | Memory limit per session container |
| `SESSION_CPU_QUOTA` | `50000` | CPU quota per session (micro-CPUs) |

### Model Selection

The frontend settings panel lets you choose from:
- `claude-sonnet-4-6` (recommended — best balance of performance and cost)
- `claude-opus-4-7` (most capable)
- `claude-haiku-4-5-20251001` (fastest/cheapest)

## Project Structure

```
browser-use-demo/
├── backend/
│   ├── main.py                 # FastAPI app, all routes, SSE, session lifecycle
│   ├── container_manager.py    # Docker SDK: spawn/stop per-session containers
│   ├── database.py             # Async SQLAlchemy + MySQL + schema migrations
│   ├── models.py               # SQLAlchemy ORM (Session, Message)
│   ├── schemas.py              # Pydantic request/response models
│   ├── tasks.py                # Background cleanup task for stale sessions
│   └── requirements.txt
├── browser_use_demo/           # Core agent library
│   ├── loop.py                 # sampling_loop() — Claude API interaction loop
│   ├── message_handler.py      # Tool execution, response processing, message building
│   └── tools/
│       ├── browser.py          # BrowserTool — 22 Playwright actions
│       └── collection.py       # ToolCollection registry
├── frontend/
│   └── index.html              # Zero-dependency SPA (HTML + CSS + vanilla JS)
├── image/
│   ├── session-entrypoint.sh   # Session container startup script
│   └── *.sh                    # Xvfb, mutter, tint2, x11vnc startup helpers
├── Dockerfile
├── docker-compose.yml          # MySQL + main-backend services
├── .env.example                # Environment variable template
└── README.md
```

### Key Files

| File | What it does |
|------|-------------|
| `backend/main.py` | API routes, SSE streaming, session lifecycle, static file serving |
| `backend/container_manager.py` | Spawns per-session Docker containers with isolated desktops |
| `browser_use_demo/loop.py` | Core agent loop: calls Claude API (native Anthropic protocol with proxy support), processes tool calls |
| `browser_use_demo/tools/browser.py` | BrowserTool: 22 Playwright-based actions (navigate, click, type, screenshot, etc.) |
| `browser_use_demo/message_handler.py` | Builds messages, executes tools, processes responses |
| `frontend/index.html` | Complete SPA frontend with SSE, VNC preview, session management |
| `image/session-entrypoint.sh` | Launches Xvfb → tint2 → mutter → x11vnc → noVNC → Chromium |

## Stop / Cleanup

```bash
# Stop the main services
docker compose down

# Clean up everything (including database volume)
docker compose down -v

# Remove session containers (if any left)
docker rm -f $(docker ps -a --filter "name=session-" -q) 2>/dev/null
```

## Safety Considerations

Browser automation carries unique risks:

1. **Isolation** — Each session runs in a separate Docker container. Avoid running as root on the host.
2. **Credentials** — Never enter personal passwords or sensitive information. The agent can see everything on screen.
3. **Domain restrictions** — Consider using an allowlist of trusted domains in production.
4. **Human confirmation** — For production use, add a confirmation step before actions with real-world consequences (purchases, deletions, etc.).
5. **Network** — Session containers share a Docker network. In production, add network policies.
6. **Bot detection** — Many sites (especially Google) block automated browsers. Respect robots.txt and terms of service.

## Credits

- [Anthropic Claude API](https://www.anthropic.com) — AI model
- [FastAPI](https://fastapi.tiangolo.com) — Backend framework
- [Playwright](https://playwright.dev) — Browser automation
- [noVNC](https://novnc.com) — Web-based VNC client
- [MySQL](https://www.mysql.com) — Database
- [Docker SDK for Python](https://docker-py.readthedocs.io) — Container orchestration
