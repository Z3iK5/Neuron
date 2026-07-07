# SPDX-License-Identifier: Apache-2.0
"""Deliver notifications to mobile push gateways (Sygnal HTTP format).

After a message-like event is persisted, :meth:`PushSender.dispatch` works out
which local users their push rules say to notify, records a notification row for
each (for ``GET /notifications`` and unread counts), and POSTs to every ``http``
pusher's gateway URL. Delivery is best-effort and must run OFF the request path
(the caller fires it as a background task): a slow or broken gateway can never
delay or fail the sender's ``/send`` response. Pushkeys and message content are
never logged.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from neuron_core import get_logger
from neuron_server import pushrules
from neuron_server.clock import now_ms
from neuron_server.federation.validation import domain_of
from neuron_server.push import evaluator
from neuron_server.rooms.events import Event
from neuron_server.storage import notifications as notif_store
from neuron_server.storage import push as push_store
from neuron_server.storage import pushers as pusher_store
from neuron_server.storage import receipts as receipts_store
from neuron_server.storage import rooms as rooms_store
from neuron_server.storage import userdata
from neuron_server.storage.database import Database

_logger = get_logger(__name__)

# Event types that generate a push. Membership events only push for an invite
# (handled specially — the recipient is the invited user, not the room's members).
_MESSAGE_TYPES = frozenset({"m.room.message", "m.room.encrypted"})

OpenGateway = Callable[[], httpx.AsyncClient]


class PushSender:
    """Evaluates push rules for a new event and POSTs to users' push gateways."""

    def __init__(
        self,
        db: Database,
        server_name: str,
        *,
        timeout: float = 10.0,
        open_gateway: OpenGateway | None = None,
    ) -> None:
        self._db = db
        self._server_name = server_name
        self._timeout = timeout
        # Injectable HTTP client factory (mirrors FederationClient.open_client) so
        # tests route gateway POSTs at an in-process fake instead of the network.
        self.open_gateway: OpenGateway = open_gateway or self._default_open

    def _default_open(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout)

    def _is_local(self, user_id: str) -> bool:
        return domain_of(user_id) == self._server_name

    async def dispatch(self, event: Event) -> None:
        """Notify the local users whose push rules match ``event``. Best-effort."""
        membership = event.content.get("membership")
        if event.type == "m.room.member":
            if membership != "invite" or event.state_key is None:
                return
            recipients = [event.state_key] if self._is_local(event.state_key) else []
        elif event.type in _MESSAGE_TYPES:
            members = await rooms_store.get_joined_members(self._db, event.room_id)
            recipients = [
                u for u in members if self._is_local(u) and u != event.sender
            ]
        else:
            return
        if not recipients:
            return

        ctx = await self._room_context(event)
        event_dict: dict[str, Any] = {
            "event_id": event.event_id,
            "room_id": event.room_id,
            "type": event.type,
            "sender": event.sender,
            "content": event.content,
            "state_key": event.state_key,
        }
        for user_id in recipients:
            await self._dispatch_to_user(user_id, event, event_dict, ctx)

    async def _room_context(self, event: Event) -> evaluator.RoomContext:
        member_count = await rooms_store.count_joined_members(self._db, event.room_id)
        pl_event = await rooms_store.get_state_event(
            self._db, event.room_id, "m.room.power_levels", ""
        )
        pl = pl_event.content if pl_event else {}
        users = pl.get("users", {}) if isinstance(pl.get("users"), dict) else {}
        sender_pl = int(users.get(event.sender, pl.get("users_default", 0)))
        levels = pl.get("notifications", {})
        notification_levels = (
            {k: int(v) for k, v in levels.items()} if isinstance(levels, dict) else {}
        )
        return evaluator.RoomContext(
            member_count=member_count,
            sender_power_level=sender_pl,
            notification_levels=notification_levels,
        )

    async def _dispatch_to_user(
        self,
        user_id: str,
        event: Event,
        event_dict: dict[str, Any],
        ctx: evaluator.RoomContext,
    ) -> None:
        rows = await push_store.get_rules(self._db, user_id)
        ruleset = pushrules.merged_ruleset(user_id, rows)
        display_name = (await userdata.get_profile(self._db, user_id)).get(
            "displayname"
        ) or user_id.split(":", 1)[0].lstrip("@")
        decision = evaluator.evaluate(
            ruleset, event_dict, display_name=str(display_name), ctx=ctx
        )
        if not decision.notify:
            return

        await notif_store.record(
            self._db,
            user_id,
            event_id=event.event_id,
            room_id=event.room_id,
            actions=decision.actions or ["notify"],
            ts=now_ms(),
            highlight=decision.highlight,
        )

        pushers = [
            p
            for p in await pusher_store.get_pushers(self._db, user_id)
            if p.kind == "http" and isinstance(p.data.get("url"), str)
        ]
        if pushers:
            await self._push_to_gateways(user_id, event, decision, pushers)

    async def _push_to_gateways(
        self,
        user_id: str,
        event: Event,
        decision: evaluator.Decision,
        pushers: list[pusher_store.Pusher],
    ) -> None:
        unread, _ = await receipts_store.get_unread_counts(
            self._db, event.room_id, user_id
        )
        tweaks: dict[str, object] = {}
        if decision.sound is not None:
            tweaks["sound"] = decision.sound
        if decision.highlight:
            tweaks["highlight"] = True

        # Group pushers by gateway URL: one gateway request carries every device
        # registered at that URL (per the push-gateway spec).
        by_url: dict[str, list[pusher_store.Pusher]] = {}
        for pusher in pushers:
            by_url.setdefault(str(pusher.data["url"]), []).append(pusher)

        for url, group in by_url.items():
            event_id_only = event.type == "m.room.encrypted" or all(
                p.data.get("format") == "event_id_only" for p in group
            )
            body = await self._build_notification(
                event, user_id, unread, tweaks, group, event_id_only=event_id_only
            )
            await self._post(url, body, user_id, group)

    async def _build_notification(
        self,
        event: Event,
        user_id: str,
        unread: int,
        tweaks: dict[str, object],
        group: list[pusher_store.Pusher],
        *,
        event_id_only: bool,
    ) -> dict[str, object]:
        devices = [
            {
                "app_id": p.app_id,
                "pushkey": p.pushkey,
                "pushkey_ts": p.ts,
                "data": p.data,
                "tweaks": tweaks,
            }
            for p in group
        ]
        prio = "high" if tweaks else "low"
        notification: dict[str, object] = {
            "event_id": event.event_id,
            "room_id": event.room_id,
            "counts": {"unread": unread},
            "devices": devices,
            "prio": prio,
        }
        if event_id_only:
            # Minimal push: only ids (used for m.room.encrypted, whose body we
            # cannot read, and for pushers requesting the event_id_only format).
            return {"notification": notification}
        notification["type"] = event.type
        notification["sender"] = event.sender
        notification["content"] = event.content
        sender_display_name = (
            await userdata.get_profile(self._db, event.sender)
        ).get("displayname")
        if sender_display_name:
            notification["sender_display_name"] = sender_display_name
        name_event = await rooms_store.get_state_event(
            self._db, event.room_id, "m.room.name", ""
        )
        if name_event and name_event.content.get("name"):
            notification["room_name"] = name_event.content["name"]
        alias_event = await rooms_store.get_state_event(
            self._db, event.room_id, "m.room.canonical_alias", ""
        )
        if alias_event and alias_event.content.get("alias"):
            notification["room_alias"] = alias_event.content["alias"]
        return {"notification": notification}

    async def _post(
        self,
        url: str,
        body: dict[str, object],
        user_id: str,
        group: list[pusher_store.Pusher],
    ) -> None:
        """POST to one gateway; on a rejected pushkey, delete the stale pusher.

        Never raises and never logs the URL, pushkeys, or message content."""
        try:
            client = self.open_gateway()
            try:
                response = await client.post(url, json=body)
                response.raise_for_status()
                data = response.json()
            finally:
                await client.aclose()
        except Exception:  # noqa: BLE001 - delivery is best-effort
            _logger.warning("push gateway delivery failed")
            return
        rejected = data.get("rejected") if isinstance(data, dict) else None
        if not isinstance(rejected, list):
            return
        for pusher in group:
            if pusher.pushkey in rejected:
                await pusher_store.delete_pusher(
                    self._db, user_id, pusher.app_id, pusher.pushkey
                )
