import asyncio
import json
from urllib.parse import parse_qs

import httpx
import pytest

from joshy.providers import AuthError, BookingNotSupported, ResyClient, SevenRoomsClient
from joshy.providers.resy import parse_resy_url
from joshy.providers.sevenrooms import parse_sevenrooms_slug


def run(coro):
    return asyncio.run(coro)


def test_parse_resy_urls():
    assert parse_resy_url("https://resy.com/cities/new-york-ny/venues/carbone") == ("new-york-ny", "carbone")
    assert parse_resy_url("https://resy.com/cities/ny/lilia?date=2026-01-01") == ("ny", "lilia")


def test_parse_sevenrooms_slugs():
    assert parse_sevenrooms_slug("https://www.sevenrooms.com/reservations/thegrill") == "thegrill"
    assert parse_sevenrooms_slug("https://www.sevenrooms.com/explore/thegrill/reservations/create/search") == "thegrill"
    assert parse_sevenrooms_slug("TheGrill") == "thegrill"


def resy_handler(state):
    def handler(request: httpx.Request) -> httpx.Response:
        state.setdefault("calls", []).append((request.method, request.url.path, request))
        path = request.url.path
        if path == "/3/auth/password":
            form = parse_qs(request.content.decode())
            if form["password"] != ["right"]:
                return httpx.Response(419, json={})
            state["logins"] = state.get("logins", 0) + 1
            return httpx.Response(200, json={"token": f"tok{state['logins']}", "first_name": "Josh",
                                             "payment_methods": [{"id": 99, "is_default": True}]})
        token = request.headers.get("x-resy-auth-token")
        if token == "expired":
            return httpx.Response(419, json={})
        if path == "/3/venue":
            return httpx.Response(200, json={"id": {"resy": 6194}, "name": "Carbone"})
        if path == "/4/find":
            return httpx.Response(200, json={"results": {"venues": [{"slots": [
                {"config": {"token": "cfg-1900", "type": "Dining Room"}, "date": {"start": "2026-12-05 19:00:00"}},
                {"config": {"token": "cfg-2130", "type": "Bar"}, "date": {"start": "2026-12-05 21:30:00"}},
            ]}]}})
        if path == "/3/details":
            body = json.loads(request.content)
            assert body["config_id"] == "cfg-1900"
            return httpx.Response(200, json={"book_token": {"value": "BT"}, "cancellation": {"display": {"policy": ["$50 fee"]}}})
        if path == "/3/book":
            form = parse_qs(request.content.decode())
            assert form["book_token"] == ["BT"]
            assert json.loads(form["struct_payment_method"][0]) == {"id": 99}
            assert request.headers["content-type"] == "application/x-www-form-urlencoded"
            return httpx.Response(201, json={"resy_token": "CONF123", "reservation_id": 555})
        return httpx.Response(404)
    return handler


def test_resy_login_find_and_book():
    state = {}
    saved = []
    client = ResyClient("key", on_credentials=saved.append, transport=httpx.MockTransport(resy_handler(state)))

    async def flow():
        creds = await client.login("me@example.com", "right")
        assert creds["token"] == "tok1" and creds["payment_method_id"] == 99
        venue = await client.resolve_venue("https://resy.com/cities/new-york-ny/venues/carbone")
        assert (venue.venue_id, venue.name) == ("6194", "Carbone")
        slots = await client.find_slots("6194", "2026-12-05", 2)
        assert [s.label for s in slots] == ["Dining Room", "Bar"]
        booking = await client.book(slots[0])
        assert booking.confirmation == "CONF123"
        assert booking.details["cancellation_policy"] == "$50 fee"
        await client.aclose()

    run(flow())
    assert saved and saved[0]["email"] == "me@example.com"
    auth = state["calls"][1][2].headers["authorization"]
    assert auth == 'ResyAPI api_key="key"'


def test_resy_bad_password():
    client = ResyClient("key", transport=httpx.MockTransport(resy_handler({})))
    with pytest.raises(AuthError):
        run(client.login("me@example.com", "wrong"))


def test_resy_relogs_in_when_token_expires():
    state = {}
    creds = {"email": "me@example.com", "password": "right", "token": "expired"}
    client = ResyClient("key", credentials=creds, transport=httpx.MockTransport(resy_handler(state)))
    slots = run(client.find_slots("6194", "2026-12-05", 2))
    assert len(slots) == 2
    assert state["logins"] == 1
    last = state["calls"][-1][2]
    assert last.headers["x-resy-auth-token"] == "tok1"


SEVENROOMS_BODY = {
    "status": 200,
    "data": {"availability": {"2026-12-05": [
        {"shift_category": "DINNER", "times": [
            {"time_iso": "2026-12-05 18:00:00", "access_persistent_id": None, "type": "book"},
            {"time_iso": "2026-12-05 19:15:00", "access_persistent_id": "abc", "type": "book",
             "public_time_slot_description": "Dining Room"},
            {"time_iso": "2026-12-05 20:00:00", "access_persistent_id": "def", "type": "request"},
        ]},
    ]}},
}


def test_sevenrooms_find_slots_filters_unbookable():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=SEVENROOMS_BODY)

    client = SevenRoomsClient(transport=httpx.MockTransport(handler))
    slots = run(client.find_slots("thegrill", "2026-12-05", 4, "19:00"))
    assert [(s.start.strftime("%H:%M"), s.label) for s in slots] == [("19:15", "Dining Room")]
    assert seen["start_date"] == "12-05-2026" and seen["party_size"] == "4" and seen["venue"] == "thegrill"
    with pytest.raises(BookingNotSupported):
        run(client.book(slots[0]))
