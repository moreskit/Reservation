"""FastAPI dashboard for Joshy."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from .config import Settings
from .db import Database
from .engine import Engine
from .notify import Notifier
from .providers import PLATFORMS, AuthError, ProviderError, ResyClient

log = logging.getLogger("joshy.web")
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_DATES = 60


def expand_dates(date_from: str, date_to: str, weekdays: list[int]) -> list[str]:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to) if date_to else start
    if end < start:
        start, end = end, start
    out: list[str] = []
    day = start
    while day <= end and len(out) < MAX_DATES:
        if not weekdays or day.weekday() in weekdays:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def _redirect(path: str = "/", msg: str = "", error: bool = False) -> RedirectResponse:
    if msg:
        path += ("&" if "?" in path else "?") + ("err=" if error else "msg=") + quote(msg)
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


def create_app(settings: Settings | None = None, engine: Engine | None = None, run_watcher: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    if engine is None:
        db = Database(settings.data_dir)
        engine = Engine(settings, db, Notifier(settings))
    db = engine.db

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if run_watcher:
            engine.start()
        yield
        if run_watcher:
            await engine.stop()

    app = FastAPI(title="Joshy", lifespan=lifespan, docs_url=None, redoc_url=None)
    basic = HTTPBasic(auto_error=False)

    def require_login(creds: HTTPBasicCredentials | None = Depends(basic)) -> None:
        if not settings.password:
            return  # no password set: only safe when bound to localhost (cli enforces this)
        ok = creds is not None and secrets.compare_digest(
            creds.password.encode(), settings.password.encode()
        )
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                headers={"WWW-Authenticate": 'Basic realm="Joshy"'},
            )

    guarded = [Depends(require_login)]

    def render(request: Request, name: str, **ctx):
        ctx.update(
            request=request,
            platforms=PLATFORMS,
            accounts=db.account_labels(),
            channels=engine.notifier.channels,
            msg=request.query_params.get("msg", ""),
            err=request.query_params.get("err", ""),
        )
        return TEMPLATES.TemplateResponse(request, name, ctx)

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse, dependencies=guarded)
    async def index(request: Request):
        watches = db.list_watches()
        order = {"active": 0, "booked": 1, "paused": 2, "expired": 3}
        watches.sort(key=lambda w: (order.get(w.status, 9), -w.id))
        return render(
            request,
            "index.html",
            watches=watches,
            events=db.recent_events(40),
            weekdays=WEEKDAYS,
            today=engine.today(),
            default_interval=settings.poll_seconds,
            link=engine.booking_link,
        )

    @app.get("/connections", response_class=HTMLResponse, dependencies=guarded)
    async def connections(request: Request):
        resy = db.get_account("resy") or {}
        return render(
            request,
            "connections.html",
            resy_email=resy.get("email", ""),
            resy_name=resy.get("name", ""),
            resy_has_card=bool(resy.get("payment_method_id")),
        )

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    # ------------------------------------------------------------------ watches
    @app.post("/watches", dependencies=guarded)
    async def add_watch(
        request: Request,
        platform: str = Form(...),
        venue: str = Form(...),
        party_size: int = Form(...),
        date_from: str = Form(...),
        date_to: str = Form(""),
        earliest: str = Form(...),
        latest: str = Form(...),
        preferred: str = Form(""),
        auto_book: str = Form(""),
        interval_secs: int = Form(60),
    ):
        form = await request.form()
        weekdays = [int(v) for v in form.getlist("weekdays")]
        if platform not in PLATFORMS:
            return _redirect("/", "Pick Resy or SevenRooms.", error=True)
        try:
            dates = expand_dates(date_from, date_to, weekdays)
            if earliest > latest:
                earliest, latest = latest, earliest
            datetime.strptime(earliest, "%H:%M")
            datetime.strptime(latest, "%H:%M")
            if preferred:
                datetime.strptime(preferred, "%H:%M")
        except ValueError:
            return _redirect("/", "Check the dates and times.", error=True)
        dates = [d for d in dates if d >= engine.today()]
        if not dates:
            return _redirect("/", "None of those dates are in the future.", error=True)
        if not 1 <= party_size <= 20:
            return _redirect("/", "Party size should be between 1 and 20.", error=True)

        provider = engine.provider(platform)
        try:
            v = await provider.resolve_venue(venue)
        except (ProviderError, ValueError) as exc:
            return _redirect("/", str(exc), error=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("venue lookup failed")
            return _redirect("/", f"Couldn't look up that restaurant: {exc}", error=True)
        finally:
            await engine._close(provider)

        wants_auto = bool(auto_book) and platform == "resy"
        watch_id = db.add_watch(
            platform=platform,
            venue_id=v.venue_id,
            venue_name=v.name,
            venue_url=v.url,
            party_size=party_size,
            dates=dates,
            earliest=earliest,
            latest=latest,
            preferred=preferred,
            auto_book=wants_auto,
            interval_secs=max(15, interval_secs),
        )
        mode = "auto-book" if wants_auto else "notify"
        db.log(
            f"Watching {v.name} ({PLATFORMS[platform]}) for {party_size}, {len(dates)} date(s), "
            f"{earliest}-{latest}, mode {mode}",
            watch_id=watch_id,
        )
        engine.schedule_now(watch_id)
        note = ""
        if wants_auto and not db.get_account("resy"):
            note = " Connect your Resy account so Joshy can book."
        return _redirect("/", f"Now watching {v.name}.{note}")

    @app.post("/watches/{watch_id}/check", dependencies=guarded)
    async def check_now(watch_id: int):
        result = await engine.check_watch(watch_id)
        return _redirect("/", f"Checked: {result}")

    @app.post("/watches/{watch_id}/pause", dependencies=guarded)
    async def pause(watch_id: int):
        db.update_watch(watch_id, status="paused")
        return _redirect("/", "Paused.")

    @app.post("/watches/{watch_id}/resume", dependencies=guarded)
    async def resume(watch_id: int):
        db.update_watch(watch_id, status="active")
        engine.schedule_now(watch_id)
        return _redirect("/", "Resumed.")

    @app.post("/watches/{watch_id}/delete", dependencies=guarded)
    async def delete(watch_id: int):
        db.delete_watch(watch_id)
        return _redirect("/", "Deleted.")

    # ------------------------------------------------------------------ connections
    @app.post("/connections/resy", dependencies=guarded)
    async def connect_resy(email: str = Form(...), password: str = Form(...)):
        client = ResyClient(
            settings.resy_api_key,
            on_credentials=lambda c: db.save_account("resy", c, label=c.get("email", "")),
        )
        try:
            creds = await client.login(email.strip(), password)
        except (AuthError, ProviderError) as exc:
            return _redirect("/connections", str(exc), error=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("resy login failed")
            return _redirect("/connections", f"Couldn't reach Resy: {exc}", error=True)
        finally:
            await client.aclose()
        engine.reset_auth_alert("resy")
        db.log(f"Connected Resy account {email.strip()}")
        card = "" if creds.get("payment_method_id") else " No card on file, so venues that need one will fail."
        return _redirect("/connections", f"Resy connected.{card}")

    @app.post("/connections/{platform}/delete", dependencies=guarded)
    async def disconnect(platform: str):
        db.delete_account(platform)
        db.log(f"Disconnected {PLATFORMS.get(platform, platform)}")
        return _redirect("/connections", "Disconnected.")

    @app.post("/notify/test", dependencies=guarded)
    async def test_notify():
        if not engine.notifier.channels:
            return _redirect("/connections", "No notification channel is configured yet.", error=True)
        failed = await engine.notifier.send("Joshy test", "Notifications are working. Bon appetit!")
        if failed:
            return _redirect("/connections", f"Failed: {', '.join(failed)}. Check the logs.", error=True)
        return _redirect("/connections", "Test notification sent.")

    return app
