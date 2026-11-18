"""SevenRooms availability checker using the public reservation widget's JSON feed.

SevenRooms has no consumer login API (its official API is for restaurants and
their partners), and checkout often involves a card hold through the venue's
payment processor. So Joshy watches availability here and, when a slot opens,
sends an urgent notification with a link that opens the booking widget on the
right date and party size. Auto-booking SevenRooms is not implemented.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode, urlparse

import httpx

from .base import Booking, BookingNotSupported, ProviderError, Slot, Venue

BASE_URL = "https://www.sevenrooms.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def parse_sevenrooms_slug(text: str) -> str:
    """Pull the venue slug from a SevenRooms link, or accept a bare slug."""
    text = text.strip()
    if "://" not in text and "/" not in text:
        if text:
            return text.lower()
        raise ProviderError("Enter a SevenRooms link or venue slug.")
    parts = [p for p in urlparse(text if "://" in text else "https://" + text).path.split("/") if p]
    # /reservations/<slug>  or  /explore/<slug>/reservations/create/search
    for marker in ("reservations", "explore"):
        if marker in parts:
            idx = parts.index(marker)
            if idx + 1 < len(parts) and parts[idx + 1] != "create":
                return parts[idx + 1].lower()
    raise ProviderError(
        "Couldn't read that SevenRooms link. Paste the booking page URL, e.g. "
        "https://www.sevenrooms.com/reservations/venue-name"
    )


class SevenRoomsClient:
    platform = "sevenrooms"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=15.0,
            transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def resolve_venue(self, text: str) -> Venue:
        slug = parse_sevenrooms_slug(text)
        # Probe the availability feed so a typo'd slug fails now, not silently later.
        today = datetime.now().strftime("%Y-%m-%d")
        await self._fetch(slug, today, 2, "19:00")
        name = slug.replace("-", " ").replace("_", " ").title()
        return Venue("sevenrooms", slug, name, f"{BASE_URL}/reservations/{slug}")

    @staticmethod
    def booking_link(venue_url: str, day: str, party_size: int) -> str:
        return f"{venue_url}?{urlencode({'date': day, 'party_size': party_size})}"

    async def _fetch(self, slug: str, day: str, party_size: int, around: str) -> dict:
        resp = await self._http.get(
            "/api-yoa/availability/widget/range",
            params={
                "venue": slug,
                "time_slot": around or "19:00",
                "party_size": party_size,
                "halo_size_interval": 16,
                "start_date": datetime.strptime(day, "%Y-%m-%d").strftime("%m-%d-%Y"),
                "num_days": 1,
                "channel": "SEVENROOMS_WIDGET",
            },
        )
        if resp.status_code == 404:
            raise ProviderError(f"SevenRooms doesn't know a venue called '{slug}'.")
        if resp.status_code >= 400:
            raise ProviderError(f"SevenRooms availability failed: HTTP {resp.status_code} {resp.text[:200]}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise ProviderError("SevenRooms returned non-JSON availability.") from exc
        if isinstance(body, dict) and body.get("status") not in (None, 200, "200", "ok", "success") and not body.get("data"):
            raise ProviderError(f"SevenRooms error: {str(body.get('msg') or body)[:200]}")
        return body

    async def find_slots(self, venue_id: str, day: str, party_size: int, around: str = "") -> list[Slot]:
        body = await self._fetch(venue_id, day, party_size, around)
        availability = ((body.get("data") or {}).get("availability")) or {}
        shifts = availability.get(day) or []
        slots: list[Slot] = []
        seen: set[str] = set()
        for shift in shifts:
            for t in shift.get("times") or []:
                if not t.get("access_persistent_id"):
                    continue  # placeholder row with nothing bookable
                if t.get("type") and t["type"] != "book":
                    continue  # "request" = waitlist-style request, not a real table
                iso = t.get("time_iso")
                if not iso:
                    continue
                try:
                    start = datetime.strptime(iso, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                label = t.get("public_time_slot_description") or shift.get("shift_category") or ""
                key = f"{iso}|{label}"
                if key in seen:
                    continue
                seen.add(key)
                slots.append(
                    Slot(
                        platform="sevenrooms",
                        venue_id=venue_id,
                        start=start,
                        party_size=party_size,
                        label=label,
                        raw={"access_persistent_id": t.get("access_persistent_id"), "day": day},
                    )
                )
        return slots

    async def book(self, slot: Slot) -> Booking:
        raise BookingNotSupported("SevenRooms slots are sent to you to book with one tap.")
