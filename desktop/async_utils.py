import asyncio
import threading


async def run_in_thread(target):
    """Run a blocking callable off the UI thread and await its result. On
    success, result["ok"] is True and result["value"] holds the return
    value; on exception, result["ok"] is False and result["error"] holds
    str(exception)."""
    result = {}

    def worker():
        try:
            result["value"] = target()
            result["ok"] = True
        except Exception as e:
            result["ok"] = False
            result["error"] = str(e)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    while thread.is_alive():
        await asyncio.sleep(0.1)
    return result


async def run_polled_job(target, args, is_running_getter, on_tick=None):
    """Start `target(*args)` (a module-level function with its own global
    "is running" flag, e.g. scheduler.run_sync_thread/run_download_thread -
    not a plain callable like run_in_thread takes) in a background thread,
    and await until `is_running_getter()` returns False. Calls `on_tick()`
    every ~0.3s while it runs, e.g. to drain a log queue into a live status
    label/panel. Adds a couple of trailing ticks after the flag flips back
    to False, since a final log line can land just before it does (the
    logging call happens right before the thread's `finally` clears the flag)."""
    threading.Thread(target=target, args=args, daemon=True).start()

    # Give the thread a moment to flip its running-flag before polling, so
    # an immediate failure can't race this loop into exiting on its first
    # iteration and skipping the "running" state entirely.
    await asyncio.sleep(0.05)

    while is_running_getter():
        if on_tick:
            on_tick()
        await asyncio.sleep(0.3)

    await asyncio.sleep(0.2)
    if on_tick:
        on_tick()
