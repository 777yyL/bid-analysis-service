"""Task-scoped diagnostic events, without credentials or document bodies."""
import asyncio
import logging
from contextvars import ContextVar

trace_callback = ContextVar('sales_trace_callback',default=None)
logger = logging.getLogger('analyzer.tasks')


def emit(phase, **details):
    callback=trace_callback.get()
    if callback:
        callback(phase,details)


async def emit_async(phase, **details):
    await asyncio.to_thread(emit,phase,**details)
