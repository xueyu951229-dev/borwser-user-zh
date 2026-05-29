"""
Background tasks for session management.

Runs periodic cleanup and reconciliation:
- Idle session timeout: deactivates sessions that haven't been updated in N minutes
- Container reconciliation: detects dead containers and marks DB sessions inactive
"""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from .container_manager import ContainerManager
from .database import AsyncSessionLocal
from .models import Session as SessionModel

logger = logging.getLogger(__name__)

# Sessions idle for this long get auto-cleaned up
SESSION_IDLE_TIMEOUT_MINUTES = 30

# How often the background task runs
CLEANUP_INTERVAL_SECONDS = 60


class SessionCleanupTask:
    """Background task that cleans up idle and stale sessions."""

    def __init__(self, container_manager: ContainerManager) -> None:
        self.container_manager = container_manager
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the background cleanup loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._cleanup_loop())
        logger.info(
            "Session cleanup task started (idle_timeout=%d min, interval=%d s).",
            SESSION_IDLE_TIMEOUT_MINUTES,
            CLEANUP_INTERVAL_SECONDS,
        )

    async def stop(self) -> None:
        """Stop the background cleanup loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Session cleanup task stopped.")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _cleanup_loop(self) -> None:
        """Run cleanup and reconciliation on a fixed interval."""
        while self._running:
            try:
                await self._cleanup_idle_sessions()
                await self._reconcile_containers()
            except Exception:
                logger.exception("Unhandled error in cleanup loop.")
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)

    # ------------------------------------------------------------------
    # Idle session cleanup
    # ------------------------------------------------------------------

    async def _cleanup_idle_sessions(self) -> None:
        """
        Deactivate sessions that have been idle too long.

        A session is considered idle if its updated_at timestamp is older
        than SESSION_IDLE_TIMEOUT_MINUTES and it has an associated container.
        """
        cutoff = datetime.utcnow() - timedelta(minutes=SESSION_IDLE_TIMEOUT_MINUTES)

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SessionModel).where(
                    SessionModel.is_active == True,
                    SessionModel.updated_at < cutoff,
                    SessionModel.container_id.isnot(None),
                )
            )
            idle_sessions = result.scalars().all()

            if not idle_sessions:
                return

            logger.info("Found %d idle session(s) to clean up.", len(idle_sessions))

            for session in idle_sessions:
                logger.info(
                    "Cleaning up idle session %s (last active: %s)...",
                    session.id,
                    session.updated_at.isoformat(),
                )
                try:
                    if session.container_id:
                        await self.container_manager.stop_session_container(
                            session.container_id
                        )
                except Exception:
                    logger.exception(
                        "Failed to stop container for idle session %s.", session.id
                    )

                session.is_active = False
                session.container_id = None
                session.container_name = None
                session.novnc_port = None
                session.cdp_url = None
                session.updated_at = datetime.utcnow()

            await db.commit()
            logger.info("Cleaned up %d idle session(s).", len(idle_sessions))

    # ------------------------------------------------------------------
    # Container reconciliation
    # ------------------------------------------------------------------

    async def _reconcile_containers(self) -> None:
        """
        Reconcile database state with actual Docker containers.

        - If a container is dead/exited but the DB says it's active → deactivate.
        - If a container is running but the DB says it's inactive → clean up.
        """
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SessionModel).where(
                    SessionModel.is_active == True,
                    SessionModel.container_id.isnot(None),
                )
            )
            active_sessions = result.scalars().all()

            for session in active_sessions:
                if not session.container_id:
                    continue
                try:
                    status = await self.container_manager.get_container_status(
                        session.container_id
                    )
                except Exception:
                    logger.warning(
                        "Could not query container %s for session %s.",
                        session.container_id,
                        session.id,
                    )
                    continue

                if status in ("exited", "dead", "not_found"):
                    logger.warning(
                        "Container %s for session %s is '%s' — deactivating session.",
                        session.container_id,
                        session.id,
                        status,
                    )
                    session.is_active = False
                    session.container_id = None
                    session.container_name = None
                    session.novnc_port = None
                    session.cdp_url = None
                    session.updated_at = datetime.utcnow()

            await db.commit()
