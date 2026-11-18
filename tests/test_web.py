from fastapi.testclient import TestClient

from joshy.engine import Engine
from joshy.providers import Venue
from joshy.web import create_app, expand_dates


def test_expand_dates_with_weekdays():
    # Dec 2099: the 4th is a Friday
    assert expand_dates("2099-12-01", "2099-12-07", [4, 5]) == ["2099-12-04", "2099-12-05"]
    assert expand_dates("2099-12-01", "", []) == ["2099-12-01"]


class Prov:
    async def resolve_venue(self, text):
        return Venue("resy", "6194", "Carbone", "https://resy.com/cities/ny/venues/carbone")


class Notifier:
    channels = []


def test_dashboard_requires_password_and_adds_watch(settings, db):
    engine = Engine(settings, db, Notifier(), provider_factory=lambda p: Prov())
    client = TestClient(create_app(settings, engine, run_watcher=False))
    assert client.get("/").status_code == 401
    auth = ("joshy", "pw")
    assert client.get("/", auth=auth).status_code == 200
    resp = client.post("/watches", auth=auth, follow_redirects=False, data={
        "platform": "resy", "venue": "https://resy.com/cities/ny/venues/carbone", "party_size": "2",
        "date_from": "2099-12-01", "date_to": "2099-12-07", "weekdays": ["4", "5"],
        "earliest": "18:00", "latest": "21:00", "preferred": "19:30", "auto_book": "1", "interval_secs": "60",
    })
    assert resp.status_code == 303 and "Now%20watching%20Carbone" in resp.headers["location"]
    (w,) = db.list_watches()
    assert w.dates == ["2099-12-04", "2099-12-05"] and w.auto_book
    page = client.get("/", auth=auth).text
    assert "Carbone" in page and "2099-12-04" in page
    assert client.get("/connections", auth=auth).status_code == 200
