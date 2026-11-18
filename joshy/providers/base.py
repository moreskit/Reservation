"""Shared types for reservation platforms."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


class ProviderError(Exception):
    """A platform request failed in a way worth logging."""


class AuthError(ProviderError):
    """Credentials are missing, wrong, or expired."""


class BookingNotSupported(ProviderError):
    """The platform can't be auto-booked. Joshy notifies with a booking link instead."""


class SlotGone(ProviderError):
    """Someone else grabbed the slot between finding and booking it."""


@dataclass
class Venue:
    platform: str
    venue_id: str
    name: str
    url: str


@dataclass
class Slot:
    platform: str
    venue_id: str
    start: datetime  # restaurant-local time
    party_size: int
    label: str = ""  # e.g. "Dining Room", "Bar"
    booking_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.start:%Y-%m-%d %H:%M}|{self.label}"

    def describe(self) -> str:
        when = self.start.strftime("%a %b %-d at %-I:%M %p")
        return f"{when}" + (f" ({self.label})" if self.label else "")


@dataclass
class Booking:
    confirmation: str
    slot: Slot
    details: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    platform: str

    async def resolve_venue(self, text: str) -> Venue: ...

    async def find_slots(self, venue_id: str, day: str, party_size: int, around: str) -> list[Slot]: ...

    async def book(self, slot: Slot) -> Booking: ...
