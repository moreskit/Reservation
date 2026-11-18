"""Resy client built on the same JSON API that resy.com's website uses.

Resy doesn't publish this API, so endpoint shapes can change without notice. The
flow is:

1. ``POST /3/auth/password``   email + password -> auth token + payment methods
2. ``GET  /4/find``            venue + day + party size -> open slots (each has a config token)
3. ``POST /3/details``         config token -> one-time ``book_token``
4. ``POST /3/book``            book_token + payment method -> reservation
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

import httpx

from .base import AuthError, Booking, ProviderError, Slot, SlotGone, Venue

BASE_URL = "https://api.resy.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def parse_resy_url(text: str) -> tuple[str, str]:
    """Return (location, slug) from a resy.com venue link.

    Handles both ``/cities/new-york-ny/venues/carbone`` and the older
    ``/cities/ny/carbone`` forms.
    """
    path = urlparse(text.strip()).path if "://" in text else text.strip()
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 4 and parts[0] == "cities" and parts[2] == "venues":
        return parts[1], parts[3]
    if len(parts) >= 3 and parts[0] == "cities":
        return parts[1], parts[2]
    raise ProviderError(
        "Couldn't read that Resy link. Paste the restaurant page URL, e.g. "
        "https://resy.com/cities/new-york-ny/venues/carbone"
    )


class ResyClient:
    platform = "resy"

    def __init__(
        self,
        api_key: str,
        credentials: dict[str, Any] | None = None,
        on_credentials: Callable[[dict[str, Any]], None] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_key = api_key
        self.creds: dict[str, Any] = dict(credentials or {})
        self._on_credentials = on_credentials
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=15.0, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ helpers
    def _headers(self, auth: bool = True) -> dict[str, str]:
        headers = {
            "Authorization": f'ResyAPI api_key="{self.api_key}"',
            "Origin": "https://resy.com",
            "Referer": "https://resy.com/",
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "X-Origin": "https://resy.com",
        }
        token = self.creds.get("token")
        if auth and token:
            headers["X-Resy-Auth-Token"] = token
            headers["X-Resy-Universal-Auth"] = token
        return headers

    async def _request(self, method: str, path: str, *, retry_auth: bool = True, **kwargs) -> Any:
        extra = kwargs.pop("headers", {})
        resp = await self._http.request(method, path, headers={**self._headers(), **extra}, **kwargs)
        if resp.status_code in (401, 419) and retry_auth and self.creds.get("password"):
            await self.login(self.creds["email"], self.creds["password"])
            return await self._request(method, path, retry_auth=False, headers=extra, **kwargs)
        if resp.status_code in (401, 419):
            raise AuthError("Resy rejected the saved login. Reconnect your Resy account.")
        if resp.status_code == 412:
            raise SlotGone("Resy says that slot is no longer available.")
        if resp.status_code >= 400:
            raise ProviderError(f"Resy {method} {path} failed: HTTP {resp.status_code} {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"Resy returned non-JSON for {path}") from exc

    # ------------------------------------------------------------------ account
    async def login(self, email: str, password: str) -> dict[str, Any]:
        resp = await self._http.post(
            "/3/auth/password",
            headers={**self._headers(auth=False), "Content-Type": "application/x-www-form-urlencoded"},
            content=urlencode({"email": email, "password": password}),
        )
        if resp.status_code in (401, 419, 403):
            raise AuthError("Resy login failed. Check your email and password.")
        if resp.status_code >= 400:
            raise ProviderError(f"Resy login failed: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        token = data.get("token")
        if not token:
            raise AuthError("Resy login didn't return a token.")
        payment_id = data.get("payment_method_id")
        methods = data.get("payment_methods") or []
        if not payment_id and methods:
            default = next((m for m in methods if m.get("is_default")), methods[0])
            payment_id = default.get("id")
        self.creds = {
            "email": email,
            "password": password,
            "token": token,
            "payment_method_id": payment_id,
            "name": " ".join(filter(None, [data.get("first_name"), data.get("last_name")])),
        }
        if self._on_credentials:
            self._on_credentials(self.creds)
        return self.creds

    # ------------------------------------------------------------------ venues
    async def resolve_venue(self, text: str) -> Venue:
        text = text.strip()
        if text.isdigit():
            return Venue("resy", text, f"Resy venue {text}", "")
        location, slug = parse_resy_url(text)
        data = await self._request("GET", "/3/venue", params={"url_slug": slug, "location": location})
        venue_id = (data.get("id") or {}).get("resy")
        if not venue_id:
            raise ProviderError(f"Resy didn't recognize the venue '{slug}'.")
        url = f"https://resy.com/cities/{location}/venues/{slug}"
        return Venue("resy", str(venue_id), data.get("name") or slug, url)

    @staticmethod
    def booking_link(venue_url: str, day: str, party_size: int) -> str:
        if not venue_url:
            return "https://resy.com"
        return f"{venue_url}?{urlencode({'date': day, 'seats': party_size})}"

    # ------------------------------------------------------------------ slots
    async def find_slots(self, venue_id: str, day: str, party_size: int, around: str = "") -> list[Slot]:
        data = await self._request(
            "GET",
            "/4/find",
            params={"lat": 0, "long": 0, "day": day, "party_size": party_size, "venue_id": venue_id},
        )
        slots: list[Slot] = []
        for venue in (data.get("results") or {}).get("venues") or []:
            for raw in venue.get("slots") or []:
                config = raw.get("config") or {}
                start_str = (raw.get("date") or {}).get("start")
                token = config.get("token")
                if not start_str or not token:
                    continue
                try:
                    start = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                slots.append(
                    Slot(
                        platform="resy",
                        venue_id=str(venue_id),
                        start=start,
                        party_size=party_size,
                        label=config.get("type") or "",
                        raw={"config_token": token, "day": day},
                    )
                )
        return slots

    # ------------------------------------------------------------------ booking
    async def book(self, slot: Slot) -> Booking:
        if not self.creds.get("token"):
            raise AuthError("Connect your Resy account before Joshy can book.")
        details = await self._request(
            "POST",
            "/3/details",
            json={
                "commit": 1,
                "config_id": slot.raw["config_token"],
                "day": slot.raw["day"],
                "party_size": slot.party_size,
            },
        )
        book_token = (details.get("book_token") or {}).get("value")
        if not book_token:
            raise SlotGone("Resy didn't hand out a booking token (slot probably taken).")

        payment_id = self.creds.get("payment_method_id")
        if not payment_id:
            methods = ((details.get("user") or {}).get("payment_methods")) or []
            payment_id = methods[0]["id"] if methods else None

        form = {"book_token": book_token, "source_id": "resy.com-venue-details"}
        if payment_id:
            form["struct_payment_method"] = json.dumps({"id": payment_id})
        result = await self._request(
            "POST",
            "/3/book",
            content=urlencode(form),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        confirmation = str(result.get("resy_token") or result.get("reservation_id") or "")
        if not confirmation:
            raise ProviderError(f"Resy booking response had no confirmation: {str(result)[:200]}")
        cancellation = (details.get("cancellation") or {}).get("display") or {}
        return Booking(
            confirmation=confirmation,
            slot=slot,
            details={
                "reservation_id": result.get("reservation_id"),
                "cancellation_policy": " ".join(cancellation.get("policy") or []),
            },
        )
