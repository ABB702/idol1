"""Wall-clock scheduler: a missed 09:00 minute is deliberately not replayed."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def local_now() -> datetime:
    return datetime.now(SHANGHAI)


class Scheduler:
    def __init__(self, callback, now=None):
        self.callback = callback
        self.now = now or local_now
        self._task = None
        self._checked = None

    async def tick(self):
        current = self.now().astimezone(SHANGHAI)
        day = current.date().isoformat()
        if current.hour == 9 and current.minute == 0 and self._checked != day:
            self._checked = day
            return await self.callback()
        return None

    def start(self):
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            try:
                await self.tick()
            except Exception:
                # The next startup/manual run can retry; never busy-loop failures.
                import logging

                logging.getLogger(__name__).exception("Daily update check failed")
            await asyncio.sleep(30)

    async def close(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
