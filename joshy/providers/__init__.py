from .base import (
    AuthError,
    Booking,
    BookingNotSupported,
    Provider,
    ProviderError,
    Slot,
    SlotGone,
    Venue,
)
from .resy import ResyClient
from .sevenrooms import SevenRoomsClient

PLATFORMS = {"resy": "Resy", "sevenrooms": "SevenRooms"}

__all__ = [
    "AuthError",
    "Booking",
    "BookingNotSupported",
    "PLATFORMS",
    "Provider",
    "ProviderError",
    "ResyClient",
    "SevenRoomsClient",
    "Slot",
    "SlotGone",
    "Venue",
]
