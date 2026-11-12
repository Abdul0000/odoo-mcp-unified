from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Any

from ..config import settings


# Bound long-running connector work so one busy flow cannot create unbounded
# daemon threads and make jobs.get/search_read sluggish. Two workers let a
# parent safe-deploy job wait for its nested module-deploy job without starving
# the executor; larger deployments can opt in to more.
_EXECUTOR = ThreadPoolExecutor(
    # Accept legacy deployments configured with 1, but never recreate the
    # parent/nested-job starvation fixed by the two-worker default.
    max_workers=max(2, settings.background_max_workers),
    thread_name_prefix="odoo_agent_job",
)


def submit_background(callback: Callable[[], Any]) -> Future[Any]:
    """Submit bounded background work and expose submission failures to callers."""
    return _EXECUTOR.submit(callback)
