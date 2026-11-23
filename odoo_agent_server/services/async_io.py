"""Drain durable thread writes before cancellation releases their owner lock."""
import asyncio

import anyio


async def durable_thread_call(function, *args):
    writer = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(writer)
    except asyncio.CancelledError as cancelled:
        # AnyIO uses level cancellation: without this shield every drain await
        # can immediately cancel again and starve the writer's completion callback.
        with anyio.CancelScope(shield=True):
            while not writer.done():
                try:
                    await asyncio.shield(writer)
                except asyncio.CancelledError:
                    pass  # Also tolerate repeated direct Task.cancel() calls.
        writer.result()
        raise cancelled
