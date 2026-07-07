# SPDX-License-Identifier: Apache-2.0
"""Tests for mobile push: pushers CRUD, the rule evaluator, gateway dispatch off
the request path, rejected-pushkey cleanup, encrypted count-only pushes, and the
/notifications list."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.push import evaluator
from neuron_server.pushrules import default_ruleset

_CS = "/_matrix/client/v3"


# --- unit: evaluator --------------------------------------------------------


def _ctx(member_count: int = 5, sender_pl: int = 0) -> evaluator.RoomContext:
    return evaluator.RoomContext(
        member_count=member_count,
        sender_power_level=sender_pl,
        notification_levels={"room": 50},
    )


def _message(body: str, sender: str = "@carol:neuron.local") -> dict[str, Any]:
    return {
        "type": "m.room.message",
        "sender": sender,
        "room_id": "!r:neuron.local",
        "content": {"msgtype": "m.text", "body": body},
    }


def test_evaluator_mention_notifies_and_highlights() -> None:
    ruleset = default_ruleset("@alice:neuron.local")
    decision = evaluator.evaluate(
        ruleset, _message("hey alice, look"), display_name="Alice Wonderland", ctx=_ctx()
    )
    assert decision.notify is True
    assert decision.highlight is True  # .m.rule.contains_user_name (localpart match)


def test_evaluator_display_name_highlights() -> None:
    ruleset = default_ruleset("@alice:neuron.local")
    decision = evaluator.evaluate(
        ruleset, _message("ping Alice Wonderland!"), display_name="Alice Wonderland",
        ctx=_ctx(),
    )
    assert decision.notify is True
    assert decision.highlight is True


def test_evaluator_plain_message_notifies_without_highlight() -> None:
    ruleset = default_ruleset("@alice:neuron.local")
    decision = evaluator.evaluate(
        ruleset, _message("just chatting"), display_name="Alice", ctx=_ctx()
    )
    assert decision.notify is True
    assert decision.highlight is False


def test_evaluator_master_rule_silences_everything() -> None:
    ruleset = default_ruleset("@alice:neuron.local")
    ruleset["override"][0]["enabled"] = True  # enable .m.rule.master
    decision = evaluator.evaluate(
        ruleset, _message("hey alice"), display_name="Alice", ctx=_ctx()
    )
    assert decision.notify is False


def test_evaluator_roomnotif_requires_power() -> None:
    ruleset = default_ruleset("@alice:neuron.local")
    body = _message("@room standup now")
    # Without power to notify the room, @room falls through to a normal message.
    weak = evaluator.evaluate(ruleset, body, display_name="Alice", ctx=_ctx(sender_pl=0))
    assert weak.highlight is False
    # With power >= notifications.room (50), .m.rule.roomnotif highlights.
    strong = evaluator.evaluate(
        ruleset, body, display_name="Alice", ctx=_ctx(sender_pl=50)
    )
    assert strong.highlight is True


def test_evaluator_one_to_one_by_member_count() -> None:
    ruleset = default_ruleset("@alice:neuron.local")
    decision = evaluator.evaluate(
        ruleset, _message("yo"), display_name="Alice", ctx=_ctx(member_count=2)
    )
    assert decision.notify is True
    assert decision.sound == "default"  # .m.rule.room_one_to_one sets a sound


# --- integration harness ----------------------------------------------------


class _FakeGateway:
    """An in-process push gateway captured via an httpx MockTransport."""

    def __init__(self, *, rejected: list[str] | None = None, delay: float = 0.0) -> None:
        self.requests: list[dict[str, Any]] = []
        self.rejected = rejected or []
        self.delay = delay
        self.hit = asyncio.Event()

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.requests.append(json.loads(request.content))
        self.hit.set()
        return httpx.Response(200, json={"rejected": self.rejected})

    def install(self, app: Any) -> None:
        def open_gateway() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

        app.state.push_sender.open_gateway = open_gateway


class _Server:
    """One homeserver with alice + bob, both joined to a shared room."""

    def __init__(self, tmp_path: Path) -> None:
        self.app = create_app(
            NeuronServerSettings(
                name="neuron.local", database_url=f"sqlite:///{tmp_path / 'hs.db'}"
            )
        )

    async def __aenter__(self) -> _Server:
        self._ctx = self.app.router.lifespan_context(self.app)
        await self._ctx.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://neuron.local"
        )
        self.alice_token, self.alice = await _register(self.client, "alice")
        self.bob_token, self.bob = await _register(self.client, "bob")
        self.alice_h = {"Authorization": f"Bearer {self.alice_token}"}
        self.bob_h = {"Authorization": f"Bearer {self.bob_token}"}
        self.room_id = await self._shared_room()
        return self

    async def __aexit__(self, *exc: object) -> None:
        for task in list(self.app.state.rooms._push_tasks):
            task.cancel()
        await self.client.aclose()
        await self._ctx.__aexit__(None, None, None)

    async def _shared_room(self) -> str:
        room_id = (
            await self.client.post(
                f"{_CS}/createRoom",
                headers=self.alice_h,
                json={"preset": "private_chat", "invite": [self.bob]},
            )
        ).json()["room_id"]
        await self.client.post(f"{_CS}/rooms/{room_id}/join", headers=self.bob_h)
        return room_id

    async def send(self, etype: str, content: dict[str, Any], txn: str) -> str:
        resp = await self.client.put(
            f"{_CS}/rooms/{self.room_id}/send/{etype}/{txn}",
            headers=self.alice_h,
            json=content,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["event_id"]

    async def drain_push(self) -> None:
        tasks = list(self.app.state.rooms._push_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def _register(client: httpx.AsyncClient, username: str) -> tuple[str, str]:
    session = (
        await client.post(
            f"{_CS}/register", json={"username": username, "password": "pw-123456"}
        )
    ).json()["session"]
    out = (
        await client.post(
            f"{_CS}/register",
            json={
                "username": username,
                "password": "pw-123456",
                "auth": {"type": "m.login.dummy", "session": session},
            },
        )
    ).json()
    return out["access_token"], out["user_id"]


async def _set_pusher(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    pushkey: str = "token-abc",
    url: str = "https://push.example/_matrix/push/v1/notify",
    app_id: str = "com.example.app",
    kind: str | None = "http",
    append: bool = False,
    data_format: str | None = None,
) -> httpx.Response:
    data: dict[str, Any] = {"url": url}
    if data_format is not None:
        data["format"] = data_format
    body: dict[str, Any] = {
        "app_id": app_id,
        "pushkey": pushkey,
        "kind": kind,
        "app_display_name": "Example",
        "device_display_name": "Phone",
        "lang": "en",
        "data": data,
        "append": append,
    }
    return await client.post(f"{_CS}/pushers/set", headers=headers, json=body)


# --- pushers CRUD -----------------------------------------------------------


async def test_pushers_crud_set_get_delete(tmp_path: Path) -> None:
    async with _Server(tmp_path) as s:
        assert (await _set_pusher(s.client, s.bob_h)).status_code == 200
        listed = (await s.client.get(f"{_CS}/pushers", headers=s.bob_h)).json()
        assert len(listed["pushers"]) == 1
        pusher = listed["pushers"][0]
        assert pusher["pushkey"] == "token-abc"
        assert pusher["kind"] == "http"
        assert pusher["data"]["url"].endswith("/notify")

        # kind=null deletes.
        assert (
            await _set_pusher(s.client, s.bob_h, kind=None)
        ).status_code == 200
        listed = (await s.client.get(f"{_CS}/pushers", headers=s.bob_h)).json()
        assert listed["pushers"] == []


async def test_pusher_http_requires_url(tmp_path: Path) -> None:
    async with _Server(tmp_path) as s:
        resp = await s.client.post(
            f"{_CS}/pushers/set",
            headers=s.bob_h,
            json={"app_id": "a", "pushkey": "k", "kind": "http", "data": {}},
        )
        assert resp.status_code == 400
        assert resp.json()["errcode"] == "M_MISSING_PARAM"


async def test_pusher_append_false_removes_pushkey_elsewhere(tmp_path: Path) -> None:
    async with _Server(tmp_path) as s:
        # Both alice and bob register the SAME pushkey (a phone re-registered).
        await _set_pusher(s.client, s.alice_h, pushkey="shared-key", append=True)
        await _set_pusher(s.client, s.bob_h, pushkey="shared-key", append=False)
        # bob's append=false must remove the key from alice.
        alice = (await s.client.get(f"{_CS}/pushers", headers=s.alice_h)).json()
        bob = (await s.client.get(f"{_CS}/pushers", headers=s.bob_h)).json()
        assert alice["pushers"] == []
        assert len(bob["pushers"]) == 1


# --- dispatch ---------------------------------------------------------------


async def test_message_triggers_gateway_post(tmp_path: Path) -> None:
    async with _Server(tmp_path) as s:
        gw = _FakeGateway()
        gw.install(s.app)
        await _set_pusher(s.client, s.bob_h)
        await s.send(
            "m.room.message", {"msgtype": "m.text", "body": "hi bob!"}, "m1"
        )
        await s.drain_push()

        assert len(gw.requests) == 1
        note = gw.requests[0]["notification"]
        assert note["type"] == "m.room.message"
        assert note["sender"] == s.alice
        assert note["content"]["body"] == "hi bob!"
        assert note["counts"]["unread"] >= 1
        devices = note["devices"]
        assert devices[0]["pushkey"] == "token-abc"
        assert devices[0]["app_id"] == "com.example.app"
        # "hi bob" mentions bob's localpart -> highlight tweak.
        assert devices[0]["tweaks"].get("highlight") is True


async def test_invite_triggers_push_to_invited_user(tmp_path: Path) -> None:
    async with _Server(tmp_path) as s:
        gw = _FakeGateway()
        gw.install(s.app)
        carol_token, carol = await _register(s.client, "carol")
        carol_h = {"Authorization": f"Bearer {carol_token}"}
        await _set_pusher(s.client, carol_h)
        resp = await s.client.post(
            f"{_CS}/rooms/{s.room_id}/invite", headers=s.alice_h,
            json={"user_id": carol},
        )
        assert resp.status_code == 200
        await s.drain_push()
        assert len(gw.requests) == 1
        note = gw.requests[0]["notification"]
        assert note["type"] == "m.room.member"
        assert note["content"]["membership"] == "invite"
        assert note["devices"][0]["pushkey"] == "token-abc"


async def test_sender_is_not_notified_of_own_message(tmp_path: Path) -> None:
    async with _Server(tmp_path) as s:
        gw = _FakeGateway()
        gw.install(s.app)
        # alice (the sender) has a pusher; she must NOT be pushed for her own msg.
        await _set_pusher(s.client, s.alice_h)
        await s.send("m.room.message", {"msgtype": "m.text", "body": "self"}, "m1")
        await s.drain_push()
        assert gw.requests == []


async def test_rejected_pushkey_deletes_pusher(tmp_path: Path) -> None:
    async with _Server(tmp_path) as s:
        gw = _FakeGateway(rejected=["token-abc"])
        gw.install(s.app)
        await _set_pusher(s.client, s.bob_h, pushkey="token-abc")
        await s.send("m.room.message", {"msgtype": "m.text", "body": "hello"}, "m1")
        await s.drain_push()
        assert len(gw.requests) == 1
        listed = (await s.client.get(f"{_CS}/pushers", headers=s.bob_h)).json()
        assert listed["pushers"] == []  # stale token cleaned up


async def test_encrypted_event_sends_count_only_push(tmp_path: Path) -> None:
    async with _Server(tmp_path) as s:
        gw = _FakeGateway()
        gw.install(s.app)
        await _set_pusher(s.client, s.bob_h)
        await s.send(
            "m.room.encrypted",
            {"algorithm": "m.megolm.v1.aes-sha2", "ciphertext": "opaque"},
            "e1",
        )
        await s.drain_push()
        assert len(gw.requests) == 1
        note = gw.requests[0]["notification"]
        assert "content" not in note  # opaque body -> count-only
        assert "type" not in note
        assert note["counts"]["unread"] >= 1
        assert note["devices"][0]["pushkey"] == "token-abc"


async def test_slow_gateway_does_not_block_send(tmp_path: Path) -> None:
    async with _Server(tmp_path) as s:
        gw = _FakeGateway(delay=3.0)
        gw.install(s.app)
        await _set_pusher(s.client, s.bob_h)
        start = time.monotonic()
        await s.send("m.room.message", {"msgtype": "m.text", "body": "hi"}, "m1")
        elapsed = time.monotonic() - start
        # The 3s gateway delay is off the request path: /send returns immediately.
        assert elapsed < 1.0
        assert not gw.hit.is_set()  # gateway not yet reached when /send returned


# --- /notifications ---------------------------------------------------------


async def test_notifications_list_read_flag_and_highlight_filter(tmp_path: Path) -> None:
    async with _Server(tmp_path) as s:
        # A plain message (notify only) and a mention (highlight).
        await s.send("m.room.message", {"msgtype": "m.text", "body": "hello"}, "m1")
        await s.drain_push()
        mention_id = await s.send(
            "m.room.message", {"msgtype": "m.text", "body": "hey bob"}, "m2"
        )
        await s.drain_push()

        listed = (
            await s.client.get(f"{_CS}/notifications", headers=s.bob_h)
        ).json()
        assert len(listed["notifications"]) == 2
        # Newest first, all unread.
        assert listed["notifications"][0]["event"]["event_id"] == mention_id
        assert all(n["read"] is False for n in listed["notifications"])

        # only=highlight keeps just the mention.
        hl = (
            await s.client.get(
                f"{_CS}/notifications", headers=s.bob_h, params={"only": "highlight"}
            )
        ).json()
        assert len(hl["notifications"]) == 1
        assert hl["notifications"][0]["event"]["event_id"] == mention_id

        # bob reads up to the mention -> both notifications become read.
        assert (
            await s.client.post(
                f"{_CS}/rooms/{s.room_id}/receipt/m.read/{mention_id}",
                headers=s.bob_h,
                json={},
            )
        ).status_code == 200
        listed = (
            await s.client.get(f"{_CS}/notifications", headers=s.bob_h)
        ).json()
        assert all(n["read"] is True for n in listed["notifications"])
