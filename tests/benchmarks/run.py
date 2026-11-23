"""Run with python -m tests.benchmarks.run; timings are local, not live Odoo SLA."""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from unittest.mock import patch

import httpx

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.routers import tools
from odoo_agent_server.schemas import JobRecord, JobStatus
from odoo_agent_server.services.store_service import JsonStore


def summarize(samples):
    samples = sorted(samples)
    return {"p50_ms": round(statistics.median(samples), 3), "p95_ms": round(samples[max(0, int(len(samples) * .95) - 1)], 3)}


def measure(callback, count):
    samples = []
    for _ in range(count):
        start = time.perf_counter()
        callback()
        samples.append((time.perf_counter() - start) * 1000)
    return summarize(samples)


async def concurrent_health(count):
    # Overlap a simulated slow SSH read with health requests through real ASGI.
    def logs(*_):
        time.sleep(.25)
        return {"status": "ok"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://benchmark") as client:
        async def health():
            start = time.perf_counter()
            response = await client.get("/health")
            response.raise_for_status()
            return (time.perf_counter() - start) * 1000
        with patch.object(tools.ClientGateService, "require_validated_client"), patch.object(tools, "_resolve_query_client", return_value=None), patch.object(tools.OdooShService, "logs_read", logs):
            log_task = asyncio.create_task(tools._dispatch("odoosh.logs_read", {"last_hours": 1}))
            await asyncio.sleep(.01)
            samples = await asyncio.gather(*(health() for _ in range(count)))
            await log_task
        return summarize(samples)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--history", type=int, default=450)
    args = parser.parse_args()
    if not 1 <= args.samples <= 1000 or not 0 <= args.history <= 5000:
        parser.error("samples must be 1..1000; history must be 0..5000")
    with tempfile.TemporaryDirectory(prefix="odoo-benchmark-") as directory, patch.object(settings, "state_dir", directory):
        store = JsonStore()
        job = store.create_job(JobRecord(job_type="benchmark", status=JobStatus.running))
        for index in range(args.history):
            store._write_job(JobRecord(id=f"benchmark-{index:08d}", job_type="history", status=JobStatus.passed))
        results = {
            "scope": "local synthetic state and ASGI; no live Odoo, SSH, or TLS latency",
            "history": args.history, "samples": args.samples,
            "job_get": measure(lambda: store.get_job(job.id), args.samples),
            "job_update": measure(lambda: store.update_job(job), args.samples),
            "job_list_10": measure(lambda: store.list_jobs(limit=10), args.samples),
            "health_during_250ms_log_read": asyncio.run(concurrent_health(min(args.samples, 32))),
        }
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
