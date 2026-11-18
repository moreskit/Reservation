import asyncio
from datetime import datetime

from joshy.engine import Engine, pick_slots
from joshy.providers import AuthError, BookingNotSupported, Booking, Slot, SlotGone


def slot(hhmm, day="2099-01-02", label=""):
    return Slot("resy", "1", datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M"), 2, label, raw={"day": day})


def test_pick_slots_window_and_preference():
    slots = [slot("17:30"), slot("18:00"), slot("19:45"), slot("20:30"), slot("22:00")]
    assert [s.start.strftime("%H:%M") for s in pick_slots(slots, "18:00", "21:00")] == ["18:00", "19:45", "20:30"]
    assert [s.start.strftime("%H:%M") for s in pick_slots(slots, "18:00", "21:00", "20:00")] == ["19:45", "20:30", "18:00"]


class FakeNotifier:
    channels = ["fake"]

    def __init__(self):
        self.sent = []

    async def send(self, title, message, *, url="", urgent=False):
        self.sent.append((title, message, url))
        return []


class FakeProvider:
    def __init__(self, slots, book_results):
        self.slots = slots
        self.book_results = list(book_results)
        self.booked = []

    async def find_slots(self, venue_id, day, party_size, around=""):
        return [s for s in self.slots if s.start.strftime("%Y-%m-%d") == day]

    async def book(self, s):
        result = self.book_results.pop(0)
        if isinstance(result, Exception):
            raise result
        self.booked.append(s)
        return Booking(result, s)


def make(settings, db, provider):
    notifier = FakeNotifier()
    return Engine(settings, db, notifier, provider_factory=lambda p: provider), notifier


def add(db, **kw):
    base = dict(platform="resy", venue_id="1", venue_name="Carbone", venue_url="https://resy.com/cities/ny/venues/carbone",
                party_size=2, dates=["2099-01-02"], earliest="18:00", latest="21:00", preferred="19:30")
    base.update(kw)
    return db.add_watch(**base)


def test_books_best_slot_and_retries_when_gone(settings, db):
    provider = FakeProvider([slot("18:00"), slot("19:30"), slot("20:00")], [SlotGone("taken"), "CONF"])
    engine, notifier = make(settings, db, provider)
    wid = add(db)
    result = asyncio.run(engine.check_watch(wid))
    assert result.startswith("BOOKED")
    assert provider.booked[0].start.strftime("%H:%M") == "20:00"  # 19:30 was gone, 20:00 next closest
    w = db.get_watch(wid)
    assert w.status == "booked" and w.booking["confirmation"] == "CONF"
    assert notifier.sent[0][0] == "Joshy got you a table!"
    # booked watches are left alone afterwards
    assert asyncio.run(engine.check_watch(wid)) == "not active"


def test_notify_only_dedupes(settings, db):
    provider = FakeProvider([slot("19:00")], [])
    engine, notifier = make(settings, db, provider)
    wid = add(db, auto_book=False)
    asyncio.run(engine.check_watch(wid))
    asyncio.run(engine.check_watch(wid))
    assert len(notifier.sent) == 1
    assert "7:00 PM" in notifier.sent[0][1]
    assert notifier.sent[0][2].endswith("?date=2099-01-02&seats=2")
    provider.slots.append(slot("20:00"))
    asyncio.run(engine.check_watch(wid))
    assert len(notifier.sent) == 2 and "8:00 PM" in notifier.sent[1][1]


def test_unsupported_booking_falls_back_to_alert(settings, db):
    provider = FakeProvider([slot("19:00")], [BookingNotSupported("no")])
    engine, notifier = make(settings, db, provider)
    wid = add(db, platform="sevenrooms", venue_url="https://www.sevenrooms.com/reservations/x")
    asyncio.run(engine.check_watch(wid))
    assert db.get_watch(wid).status == "active"
    assert notifier.sent and "party_size=2" in notifier.sent[0][2]


def test_past_dates_expire(settings, db):
    engine, _ = make(settings, db, FakeProvider([], []))
    wid = add(db, dates=["2000-01-01"])
    asyncio.run(engine.check_watch(wid))
    assert db.get_watch(wid).status == "expired"


def test_auth_error_alerts_once(settings, db):
    class Broken(FakeProvider):
        async def find_slots(self, *a, **k):
            raise AuthError("bad login")

    engine, notifier = make(settings, db, Broken([], []))
    wid = add(db)
    asyncio.run(engine.check_watch(wid))
    asyncio.run(engine.check_watch(wid))
    assert len(notifier.sent) == 1
    assert db.get_watch(wid).status == "active"


def test_credentials_encrypted_at_rest(db, settings):
    db.save_account("resy", {"email": "a@b.c", "password": "hunter2"})
    raw = (settings.data_dir / "joshy.db").read_bytes()
    assert b"hunter2" not in raw
    assert db.get_account("resy")["password"] == "hunter2"
