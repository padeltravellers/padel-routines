"""Helpers HTTP para llamar a la Routines API del Worker pt-whatsapp-bot.

Uso desde un agente Claude (RemoteTrigger):

    import os
    from lib.api import RoutinesAPI

    api = RoutinesAPI(
        base=os.environ["WORKER_BASE_URL"],
        token=os.environ["ROUTINES_API_TOKEN"],
    )
    insights = api.meta_insights(days=7, level="ad")
    api.telegram_send("Resumen semanal Meta Ads:\n...")
"""
from __future__ import annotations
import json
import urllib.request
import urllib.parse
from typing import Any


class RoutinesAPI:
    def __init__(self, base: str, token: str, timeout: int = 30) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    # -------- low level --------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        qs = ("?" + urllib.parse.urlencode(params)) if params else ""
        req = urllib.request.Request(
            f"{self.base}{path}{qs}",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "content-type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    # -------- high level --------

    def health(self) -> dict:
        return self._get("/api/health")

    def meta_insights(
        self, days: int = 7, level: str = "ad", include_paused: bool = False
    ) -> dict:
        return self._get(
            "/api/meta/insights",
            {"days": days, "level": level, "include_paused": str(include_paused).lower()},
        )

    def conversations_recent(
        self, hours: int = 24, limit: int = 50, include_messages: bool = True
    ) -> dict:
        return self._get(
            "/api/conversations/recent",
            {
                "hours": hours,
                "limit": limit,
                "include_messages": str(include_messages).lower(),
            },
        )

    def leads_state(self) -> dict:
        return self._get("/api/leads/state")

    def learned_rules(self, status: str = "all") -> dict:
        return self._get("/api/learned-rules", {"status": status})

    def telegram_send(
        self,
        text: str,
        chat_id: str | None = None,
        topic_id: int | None = None,
        markdown: bool = False,
    ) -> dict:
        body: dict = {"text": text, "markdown": markdown}
        if chat_id:
            body["chat_id"] = chat_id
        if topic_id:
            body["topic_id"] = topic_id
        return self._post("/api/telegram/send", body)
