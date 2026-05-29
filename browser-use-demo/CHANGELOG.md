
# Modifications to Microsoft Playwright Source

This file tracks all modifications made to files derived from or inspired by Microsoft Playwright source code.

## Modified Files

### browser_use_demo/browser_tool_utils/browser_dom_script.js
- **Date Modified**: 9/23/25
- **Original Source**: https://github.com/microsoft/playwright/blob/main/packages/injected/src/ariaSnapshot.ts
- **Nature of Changes**: Adapted Playwright's accessibility tree generation for use with browser tools API. Implemented accessibility tree extraction with element reference tracking, visibility filtering, and YAML-formatted output.

### browser_use_demo/browser_tool_utils/browser_element_script.js
- **Date Modified**: 9/23/25
- **Original Source**: Microsoft Playwright element interaction patterns
- **Nature of Changes**: Implemented element finding and interaction logic inspired by Playwright's approach to reliable element targeting and coordinate calculation.

### browser_use_demo/tools/browser.py
- **Date Modified**: 9/23/25
- **Original Source**: Microsoft Playwright click emulation implementation
- **Nature of Changes**: Click emulation methods developed with reference to Playwright source code during debugging to ensure reliable mouse interactions.
- **Date Modified**: 10/6/25
- **Nature of Changes**:
  - Fixed incorrect path to browser_tool_utils directory. Changed from `Path(__file__).parent / "browser_tool_utils"` to `Path(__file__).parent.parent / "browser_tool_utils"` to correctly locate JavaScript utility files.
  - Fixed missing `cdp_url` attribute initialization in `__init__` method to prevent AttributeError in cleanup method.
  - Fixed incorrect import path for browser_key_map. Changed from `.browser_tool_utils.browser_key_map` to `..browser_tool_utils.browser_key_map` to correctly import the KEY_MAP.
- **Date Modified**: 10/14/25
- **Nature of Changes**:
  - Enhanced `_scroll` and `_scroll_to` methods to return screenshots after scrolling actions. Added 0.5s stabilization delay before taking screenshots to show the new viewport content. This provides visual feedback to the model after scroll actions, consistent with the navigate action behavior.
- **Date Modified**: 12/19/25
- **Nature of Changes**:
  - Added `hover` action to move mouse cursor without clicking using Playwright's `mouse.move()` API. Useful for revealing tooltips, dropdown menus, or triggering hover states.
  - Added `execute_js` action to execute JavaScript code in page context using Playwright's `page.evaluate()` API. Returns the result of the last expression.
- **Date Modified**: 1/18/26
- **Nature of Changes**:
  - Added clarifying comment in the `options` property explaining that this implementation uses fixed 1920x1080 dimensions with empirical coordinate correction, and directing users to the "Handle coordinate scaling" section in the computer use documentation for the recommended client-side downscaling approach.
- **Date Modified**: 5/29/26
- **Nature of Changes**:
  - Added CDP (Chrome DevTools Protocol) connection mode. `__init__` now accepts optional `cdp_url` parameter. When provided, `_ensure_browser` connects to remote Chromium via `playwright.chromium.connect_over_cdp()` instead of launching locally. This enables per-session Docker container isolation where each session gets its own Chromium instance.
  - Local launch mode preserved as fallback when `cdp_url` is None.

### Per-Session Container Isolation (Multiple Files)
- **Date Modified**: 5/29/26
- **Files Added**: `image/session-entrypoint.sh`, `backend/container_manager.py`, `backend/tasks.py`
- **Files Modified**: `Dockerfile`, `docker-compose.yml`, `backend/main.py`, `backend/models.py`, `backend/database.py`, `backend/schemas.py`, `backend/requirements.txt`, `browser_use_demo/tools/browser.py`
- **Nature of Changes**:
  - Implemented per-session Docker container isolation to solve the VNC stream sharing concurrency issue.
  - Each session now gets its own Docker container with isolated Xvfb, x11vnc, noVNC, and Chromium with CDP enabled.
  - Main backend container runs FastAPI only — no longer starts Xvfb/VNC/Chrome locally.
  - ContainerManager (`backend/container_manager.py`) handles Docker container lifecycle via Docker SDK.
  - SessionCleanupTask (`backend/tasks.py`) runs periodic idle session cleanup and container reconciliation.
  - Port allocation: each session gets a unique noVNC host port from range 6081-6180.
  - CDP (port 9222) and VNC (port 5900) remain internal to the Docker network.
  - Backward compatible: falls back to local browser mode when Docker socket is unavailable.

