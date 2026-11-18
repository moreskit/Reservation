"""Push notifications: ntfy, Pushover and email. Every message is also logged."""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

import httpx

from .config import Settings

log = logging.getLogger("joshy.notify")


class Notifier:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings
        self._transport = transport

    @property
    def channels(self) -> list[str]:
        out = []
        if self.s.ntfy_topic:
            out.append("ntfy")
        if self.s.pushover_token and self.s.pushover_user:
            out.append("pushover")
        if self.s.smtp_host and self.s.email_to:
            out.append("email")
        return out

    async def send(self, title: str, message: str, *, url: str = "", urgent: bool = False) -> list[str]:
        """Send to every configured channel. Returns the channels that failed."""
        log.info("NOTIFY %s: %s %s", title, message, url)
        failures: list[str] = []
        async with httpx.AsyncClient(timeout=10.0, transport=self._transport) as http:
            if "ntfy" in self.channels:
                try:
                    headers = {"Title": title, "Priority": "urgent" if urgent else "default", "Tags": "fork_and_knife"}
                    if url:
                        headers["Click"] = url
                        headers["Actions"] = f"view, Open, {url}"
                    r = await http.post(f"{self.s.ntfy_server}/{self.s.ntfy_topic}", content=message.encode(), headers=headers)
                    r.raise_for_status()
                except Exception as exc:  # noqa: BLE001
                    log.warning("ntfy failed: %s", exc)
                    failures.append("ntfy")
            if "pushover" in self.channels:
                try:
                    data = {
                        "token": self.s.pushover_token,
                        "user": self.s.pushover_user,
                        "title": title,
                        "message": message,
                        "priority": 1 if urgent else 0,
                    }
                    if url:
                        data["url"] = url
                    r = await http.post("https://api.pushover.net/1/messages.json", data=data)
                    r.raise_for_status()
                except Exception as exc:  # noqa: BLE001
                    log.warning("pushover failed: %s", exc)
                    failures.append("pushover")
        if "email" in self.channels:
            try:
                await asyncio.to_thread(self._send_email, title, message + (f"\n\n{url}" if url else ""))
            except Exception as exc:  # noqa: BLE001
                log.warning("email failed: %s", exc)
                failures.append("email")
        return failures

    def _send_email(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.s.smtp_user or self.s.email_to
        msg["To"] = self.s.email_to
        msg.set_content(body)
        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=15) as smtp:
            smtp.starttls()
            if self.s.smtp_user:
                smtp.login(self.s.smtp_user, self.s.smtp_password)
            smtp.send_message(msg)
