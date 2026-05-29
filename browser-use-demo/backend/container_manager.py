"""
Session container lifecycle manager using Docker SDK.

Handles creating, monitoring, and cleaning up per-session Docker containers.
Each session container runs an isolated desktop environment (Xvfb + VNC + noVNC + Chromium).
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import docker
import httpx
from docker.errors import APIError, ImageNotFound, NotFound

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NOVNC_PORT_MIN = int(__import__("os").getenv("NOVNC_PORT_MIN", "6081"))
NOVNC_PORT_MAX = int(__import__("os").getenv("NOVNC_PORT_MAX", "6180"))

SESSION_IMAGE = __import__("os").getenv("SESSION_IMAGE", "browser-use-demo:latest")
SESSION_NETWORK = __import__("os").getenv("SESSION_NETWORK", "browser-use-net")

CDP_PORT = 9222
VNC_PORT = 5900
NOVNC_CONTAINER_PORT = 6080
DISPLAY_NUM = 1
DISPLAY_WIDTH = 1920
DISPLAY_HEIGHT = 1080

SESSION_MEM_LIMIT = __import__("os").getenv("SESSION_MEM_LIMIT", "2g")
SESSION_CPU_QUOTA = int(__import__("os").getenv("SESSION_CPU_QUOTA", "50000"))  # micro-CPUs


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SessionContainerInfo:
    """Metadata about a running session container."""

    container_id: str
    container_name: str
    novnc_host_port: int
    cdp_url: str
    status: str = "running"


# ---------------------------------------------------------------------------
# Container Manager
# ---------------------------------------------------------------------------


class ContainerManager:
    """
    Manages Docker session containers via the Docker SDK for Python.

    Responsibilities:
    - Create session containers with isolated desktop environments
    - Allocate unique host ports for noVNC
    - Health-check CDP readiness before returning
    - Stop/remove containers on session cleanup
    - Reconcile orphaned containers
    """

    def __init__(self) -> None:
        try:
            self.client = docker.from_env()
        except docker.errors.DockerException as e:
            raise RuntimeError(
                f"Cannot connect to Docker daemon: {e}. "
                "Ensure /var/run/docker.sock is mounted."
            ) from e
        self._lock = asyncio.Lock()
        self._ensure_image()
        self._ensure_network()

    # ------------------------------------------------------------------
    # Initialization helpers
    # ------------------------------------------------------------------

    def _ensure_image(self) -> None:
        """Verify the session image exists locally."""
        try:
            self.client.images.get(SESSION_IMAGE)
            logger.info("Session image '%s' found.", SESSION_IMAGE)
        except ImageNotFound:
            raise RuntimeError(
                f"Session image '{SESSION_IMAGE}' not found. "
                "Build it first: docker compose build"
            )

    def _ensure_network(self) -> None:
        """Ensure the shared Docker network exists so containers can communicate."""
        try:
            self.client.networks.get(SESSION_NETWORK)
            logger.info("Docker network '%s' found.", SESSION_NETWORK)
        except NotFound:
            logger.info("Creating Docker network '%s'...", SESSION_NETWORK)
            self.client.networks.create(SESSION_NETWORK, driver="bridge")

    # ------------------------------------------------------------------
    # Port allocation
    # ------------------------------------------------------------------

    def _find_available_port(self) -> int:
        """
        Find an available host port for noVNC mapping.

        Scans existing session containers to detect in-use ports, then picks
        the lowest free port in [NOVNC_PORT_MIN, NOVNC_PORT_MAX].
        """
        used_ports: set[int] = set()

        try:
            for container in self.client.containers.list(
                all=True, filters={"name": "session-"}
            ):
                ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})
                if ports:
                    for _container_port, host_bindings in ports.items():
                        if host_bindings:
                            for binding in host_bindings:
                                try:
                                    used_ports.add(int(binding["HostPort"]))
                                except (KeyError, ValueError):
                                    pass
        except APIError as e:
            logger.warning("Could not list containers for port scan: %s", e)

        for port in range(NOVNC_PORT_MIN, NOVNC_PORT_MAX + 1):
            if port not in used_ports:
                return port

        raise RuntimeError(
            f"No available ports in range {NOVNC_PORT_MIN}-{NOVNC_PORT_MAX}. "
            "Free up ports or expand the range via NOVNC_PORT_MIN/MAX env vars."
        )

    # ------------------------------------------------------------------
    # Container lifecycle
    # ------------------------------------------------------------------

    async def create_session_container(self, session_id: str) -> SessionContainerInfo:
        """
        Create and start a new session container.

        1. Allocate a host port for noVNC.
        2. Run the container with Xvfb + VNC + noVNC + Chromium (CDP).
        3. Wait for CDP to become healthy.
        4. Return container metadata.

        Raises RuntimeError on any failure (container is cleaned up on failure).
        """
        async with self._lock:
            novnc_port = self._find_available_port()

        container_name = f"session-{session_id[:12]}"

        env_vars = {
            "DISPLAY_NUM": str(DISPLAY_NUM),
            "WIDTH": str(DISPLAY_WIDTH),
            "HEIGHT": str(DISPLAY_HEIGHT),
            "CDP_PORT": str(CDP_PORT),
            "VNC_PORT": str(VNC_PORT),
            "NOVNC_PORT": str(NOVNC_CONTAINER_PORT),
        }

        logger.info(
            "Creating session container '%s' (session=%s, novnc_host_port=%d)...",
            container_name,
            session_id,
            novnc_port,
        )

        container = None
        try:
            container = self.client.containers.run(
                image=SESSION_IMAGE,
                name=container_name,
                detach=True,
                network=SESSION_NETWORK,
                environment=env_vars,
                ports={
                    f"{NOVNC_CONTAINER_PORT}/tcp": novnc_port,
                    # VNC (5900) and CDP (9222) are internal-only —
                    # accessed via Docker network, no host mapping.
                },
                entrypoint=["./session-entrypoint.sh"],
                mem_limit=SESSION_MEM_LIMIT,
                cpu_quota=SESSION_CPU_QUOTA,
                labels={
                    "app": "browser-use-demo",
                    "session_id": session_id,
                    "managed_by": "container_manager",
                },
            )

            # Reload to get full metadata (especially container.id and IP)
            container.reload()

            # Use container IP instead of hostname for CDP URL.
            # Chrome's DevTools Protocol server rejects requests where the HTTP
            # Host header contains a non-IP/non-localhost hostname (HTTP 500).
            # Docker DNS resolves container names, but the resolved hostname in
            # the Host header triggers Chrome's security check.
            container_ip = container.attrs["NetworkSettings"]["Networks"][SESSION_NETWORK]["IPAddress"]
            cdp_url = f"http://{container_ip}:{CDP_PORT}"
            await self._wait_for_cdp(cdp_url, timeout=30)

            logger.info(
                "Session container '%s' ready (cdp=%s, novnc=%d).",
                container_name,
                cdp_url,
                novnc_port,
            )

            return SessionContainerInfo(
                container_id=container.id,
                container_name=container_name,
                novnc_host_port=novnc_port,
                cdp_url=cdp_url,
                status="running",
            )

        except Exception:
            logger.exception("Failed to create session container for %s", session_id)
            if container is not None:
                try:
                    container.stop(timeout=5)
                    container.remove(force=True)
                except Exception:
                    logger.warning(
                        "Could not clean up failed container '%s'.", container_name
                    )
            raise RuntimeError(
                f"Failed to create session container for {session_id}"
            )

    async def _wait_for_cdp(self, cdp_url: str, timeout: int = 30) -> None:
        """
        Poll the CDP /json/version endpoint until it responds 200.

        Raises TimeoutError if not ready within *timeout* seconds.
        """
        url = f"{cdp_url}/json/version"
        async with httpx.AsyncClient() as client:
            for _step in range(timeout):
                await asyncio.sleep(1)
                try:
                    resp = await client.get(url, timeout=2.0)
                    if resp.status_code == 200:
                        logger.info("CDP ready at %s", url)
                        return
                except Exception:
                    pass
        raise TimeoutError(f"CDP did not become ready at {url} within {timeout}s")

    async def stop_session_container(self, container_id: str) -> None:
        """Stop and remove a session container by its Docker ID."""
        try:
            container = self.client.containers.get(container_id)
            logger.info("Stopping container %s (%s)...", container_id, container.name)
            container.stop(timeout=10)
            container.remove(force=True)
            logger.info("Removed container %s.", container_id)
        except NotFound:
            logger.warning("Container %s not found (already removed?).", container_id)
        except APIError as e:
            logger.error("Docker API error removing container %s: %s", container_id, e)
            raise

    async def get_container_status(self, container_id: str) -> str:
        """Return the Docker status string for a container."""
        try:
            container = self.client.containers.get(container_id)
            return container.status  # "running", "exited", "dead", ...
        except NotFound:
            return "not_found"

    async def cleanup_all_sessions(self) -> None:
        """Remove ALL session containers (called at application shutdown)."""
        try:
            containers = self.client.containers.list(
                all=True,
                filters={"label": "managed_by=container_manager"},
            )
        except APIError:
            logger.warning("Could not list session containers during cleanup.")
            return

        for container in containers:
            try:
                logger.info("Shutdown: removing container %s...", container.id)
                container.stop(timeout=5)
                container.remove(force=True)
            except Exception as e:
                logger.warning(
                    "Error cleaning up container %s: %s", container.id, e
                )

    async def list_session_containers(self) -> list[dict]:
        """List all running session containers with metadata."""
        try:
            containers = self.client.containers.list(
                filters={"label": "managed_by=container_manager"},
            )
        except APIError:
            logger.warning("Could not list session containers.")
            return []

        result = []
        for c in containers:
            ports = c.attrs.get("NetworkSettings", {}).get("Ports", {})
            novnc_port = None
            port_key = f"{NOVNC_CONTAINER_PORT}/tcp"
            if port_key in ports and ports[port_key]:
                try:
                    novnc_port = int(ports[port_key][0]["HostPort"])
                except (KeyError, ValueError, IndexError):
                    pass

            result.append(
                {
                    "container_id": c.id,
                    "container_name": c.name,
                    "status": c.status,
                    "novnc_port": novnc_port,
                }
            )
        return result
