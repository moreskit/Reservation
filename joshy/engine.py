"""The watcher loop: poll each active watch, book or notify when a matching slot opens."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, time as dtime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .config import Settings
from .db import Database, Watch, now_iso
from .notify import Notifier
from .providers import (
    AuthError,
    BookingNotSupported,
    PLATFORMS,
    ProviderError,
    ResyClient,
    SevenRoomsClient,
    Slot,
    SlotGone,
)

log = logging.getLogger("joshy.engine")

MAX_BOOK_ATTEMPTS = 3


def _parse_hhmm(value: str) -> dtime:
    return datetime.strptime(value, "%H:%M").time()


def pick_slots(slots: list[Slot], earliest: str, latest: str, preferred: str = "") -> list[Slot]:
    """Slots inside [earliest, latest], best first (closest to preferred, else earliest)."""
    lo, hi = _parse_hhmm(earliest), _parse_hhmm(latest)
    inside = [s for s in slots if lo <= s.start.time() <= hi]
    if preferred:
        p = _parse_hhmm(preferred)
        target = p.hour * 60 + p.minute

        def distance(s: Slot) -> tuple[int, int]:
            m = s.start.hour * 60 + s.start.minute
            return (abs(m - target), m)

        return sorted(inside, key=distance)
    return sorted(inside, key=lambda s: s.start)


class Engine:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        notifier: Notifier,
        provider_factory: Callable[[str], Any] | None = None,
    ):
        self.s = settings
        self.db = db
        self.notifier = notifier
        self._provider_factory = provider_factory
        self._next_due: dict[int, float] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._auth_alerted: set[str] = set()

    # ------------------------------------------------------------------ providers
    def provider(self, platform: str):
        if self._provider_factory:
            return self._provider_factory(platform)
        if platform == "resy":
            return ResyClient(
                self.s.resy_api_key,
                credentials=self.db.get_account("resy"),
                on_credentials=lambda c: self.db.save_account("resy", c, label=c.get("email", "")),
            )
        if platform == "sevenrooms":
            return SevenRoomsClient()
        raise ValueError(f"Unknown platform {platform}")

    @staticmethod
    async def _close(provider) -> None:
        close = getattr(provider, "aclose", None)
        if close:
            await close()

    def booking_link(self, watch: Watch, day: str) -> str:
        cls = ResyClient if watch.platform == "resy" else SevenRoomsClient
        return cls.booking_link(watch.venue_url, day, watch.party_size)

    def reset_auth_alert(self, platform: str) -> None:
        self._auth_alerted.discard(platform)

    # ------------------------------------------------------------------ one check
    def today(self) -> str:
        return datetime.now(ZoneInfo(self.s.timezone)).strftime("%Y-%m-%d")

    async def check_watch(self, watch_id: int) -> str:
        lock = self._locks.setdefault(watch_id, asyncio.Lock())
        async with lock:
            watch = self.db.get_watch(watch_id)
            if not watch or watch.status != "active":
                return "not active"
            try:
                result = await self._check(watch)
            except AuthError as exc:
                result = f"Login problem: {exc}"
                self.db.log(result, watch_id=watch.id, level="error")
                if watch.platform not in self._auth_alerted:
                    self._auth_alerted.add(watch.platform)
                    await self.notifier.send(
                        f"Joshy: {PLATFORMS[watch.platform]} login needs attention",
                        str(exc),
                        urgent=True,
                    )
            except (ProviderError, Exception) as exc:  # noqa: BLE001 - keep the loop alive
                result = f"Check failed: {exc}"
                log.exception("check failed for watch %s", watch_id)
                self.db.log(result, watch_id=watch.id, level="warning")
            self.db.update_watch(watch.id, last_checked=now_iso(), last_result=result[:500])
            return result

    async def _check(self, watch: Watch) -> str:
        today = self.today()
        upcoming = [d for d in watch.dates if d >= today]
        if not upcoming:
            self.db.update_watch(watch.id, status="expired")
            self.db.log(f"All dates passed for {watch.venue_name}. Watch expired.", watch_id=watch.id)
            return "Expired: all dates have passed"
        if upcoming != watch.dates:
            self.db.update_watch(watch.id, dates=upcoming)

        provider = self.provider(watch.platform)
        try:
            summary: list[str] = []
            for i, day in enumerate(upcoming):
                if i:
                    await asyncio.sleep(random.uniform(0.6, 1.5))  # be polite between requests
                found = await provider.find_slots(
                    watch.venue_id, day, watch.party_size, watch.preferred or watch.earliest
                )
                matches = pick_slots(found, watch.earliest, watch.latest, watch.preferred)
                if not matches:
                    summary.append(f"{day}: {len(found)} open, none in window")
                    continue

                if watch.auto_book:
                    booked = await self._try_book(watch, provider, matches, day)
                    if booked:
                        return booked
                await self._notify_open(watch, day, matches)
                summary.append(f"{day}: {len(matches)} matching")
            return "; ".join(summary)
        finally:
            await self._close(provider)

    async def _try_book(self, watch: Watch, provider, matches: list[Slot], day: str) -> str | None:
        for slot in matches[:MAX_BOOK_ATTEMPTS]:
            try:
                booking = await provider.book(slot)
            except BookingNotSupported:
                return None  # fall through to "go book it" notification
            except SlotGone as exc:
                self.db.log(f"Missed {slot.describe()}: {exc}", watch_id=watch.id, level="warning")
                continue
            info = {
                "confirmation": booking.confirmation,
                "when": slot.start.strftime("%Y-%m-%d %H:%M"),
                "label": slot.label,
                "party_size": slot.party_size,
                **booking.details,
            }
            self.db.update_watch(watch.id, status="booked", booking=info)
            msg = f"Booked {watch.venue_name} for {slot.party_size} on {slot.describe()}."
            self.db.log(msg + f" Confirmation {booking.confirmation}", watch_id=watch.id, level="success")
            await self.notifier.send(
                "Joshy got you a table!", msg, url=self.booking_link(watch, day), urgent=True
            )
            return f"BOOKED {slot.describe()}"
        return None

    async def _notify_open(self, watch: Watch, day: str, matches: list[Slot]) -> None:
        fresh = [s for s in matches if s.key not in watch.notified]
        if not fresh:
            return
        times = ", ".join(s.start.strftime("%-I:%M %p") for s in sorted(fresh, key=lambda s: s.start)[:6])
        date_label = fresh[0].start.strftime("%a %b %-d")
        why = " (couldn't auto-book, grab it fast)" if watch.auto_book and watch.platform == "resy" else ""
        msg = f"{watch.venue_name}, party of {watch.party_size}, {date_label}: {times}{why}"
        self.db.log(f"Open: {msg}", watch_id=watch.id, level="success")
        await self.notifier.send(
            f"Table open at {watch.venue_name}", msg, url=self.booking_link(watch, day), urgent=True
        )
        watch.notified = (watch.notified + [s.key for s in fresh])[-200:]
        self.db.update_watch(watch.id, notified=watch.notified)

    # ------------------------------------------------------------------ loop
    def schedule_now(self, watch_id: int) -> None:
        self._next_due[watch_id] = 0

    async def run_forever(self) -> None:
        log.info("Joshy watcher started")
        while not self._stop.is_set():
            now = time.monotonic()
            for watch in self.db.list_watches(status="active"):
                due = self._next_due.get(watch.id, 0)
                if now < due:
                    continue
                interval = max(15, watch.interval_secs)
                self._next_due[watch.id] = now + interval * random.uniform(0.85, 1.15)
                await self.check_watch(watch.id)
                if self._stop.is_set():
                    break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self.run_forever())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
