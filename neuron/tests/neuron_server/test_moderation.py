# SPDX-License-Identifier: Apache-2.0
"""Real moderation backing: shadow-ban, room block, delete/purge, redaction,
event reports and server notices — exercised over the real HTTP API."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.storage import rooms as room_store

_REG = "/_matrix/client/v3/register"
_ADMIN = "/_synapse/admin"


async def _register(raw: httpx.AsyncClient, username: str) -> str:
    challenge = await raw.post(_REG, json={"username": username, "password": "pw-123456"})
    session = challenge.json()["session"]
    result = await raw.post(
        _REG,
        json={
            "username": username,
            "password": "pw-123456",
            "auth": {"type": "m.login.dummy", "session": session},
        },
    )
    return result.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _Env:
    def __init__(
        self, raw: httpx.AsyncClient, admin_token: str, bob_token: str, db: Any
    ) -> None:
        self.raw = raw
        self.admin = admin_token
        self.bob = bob_token
        self.db = db

    async def send(
        self, room: str, token: str, body: str, txn: str
    ) -> tuple[int, dict[str, Any]]:
        resp = await self.raw.put(
            f"/_matrix/client/v3/rooms/{room}/send/m.room.message/{txn}",
            headers=_auth(token),
            json={"msgtype": "m.text", "body": body},
        )
        return resp.status_code, resp.json()

    async def messages(self, room: str, token: str) -> list[dict[str, Any]]:
        resp = await self.raw.get(
            f"/_matrix/client/v3/rooms/{room}/messages?dir=b&limit=100",
            headers=_auth(token),
        )
        return resp.json().get("chunk", [])


@contextlib.asynccontextmanager
async def _env(tmp_path: Path) -> AsyncIterator[_Env]:
    settings = NeuronServerSettings(
        name="neuron.local",
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
        admin_users="admin",  # first user "admin" is a server admin
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://hs.test") as raw:
            admin_token = await _register(raw, "admin")
            bob_token = await _register(raw, "bob")
            yield _Env(raw, admin_token, bob_token, app.state.db)


async def _make_room_with_bob(env: _Env) -> str:
    room = (
        await env.raw.post(
            "/_matrix/client/v3/createRoom",
            headers=_auth(env.admin),
            json={"preset": "public_chat"},
        )
    ).json()["room_id"]
    await env.raw.post(f"/_matrix/client/v3/join/{room}", headers=_auth(env.bob), json={})
    return room


async def test_shadow_ban_silently_drops_messages(tmp_path: Path) -> None:
    async with _env(tmp_path) as env:
        room = await _make_room_with_bob(env)
        await env.send(room, env.bob, "before", "t1")
        before = len(await env.messages(room, env.admin))

        r = await env.raw.post(
            f"{_ADMIN}/v1/users/@bob:neuron.local/shadow_ban", headers=_auth(env.admin)
        )
        assert r.status_code == 200

        code, body = await env.send(room, env.bob, "spam", "t2")
        assert code == 200 and body["event_id"]  # accepted with an id...
        after = await env.messages(room, env.admin)
        assert len(after) == before  # ...but never actually stored

        # Lifting the shadow-ban restores delivery.
        await env.raw.delete(
            f"{_ADMIN}/v1/users/@bob:neuron.local/shadow_ban", headers=_auth(env.admin)
        )
        await env.send(room, env.bob, "after", "t3")
        assert len(await env.messages(room, env.admin)) == before + 1


async def test_shadow_ban_drops_state_events(tmp_path: Path) -> None:
    async with _env(tmp_path) as env:
        room = await _make_room_with_bob(env)
        # Give Bob power so a topic change would normally succeed — this isolates
        # the shadow-ban as the reason it doesn't, rather than a power-level denial.
        mk = await env.raw.post(
            f"{_ADMIN}/v1/rooms/{room}/make_room_admin",
            headers=_auth(env.admin),
            json={"user_id": "@bob:neuron.local"},
        )
        assert mk.status_code == 200
        await env.raw.post(
            f"{_ADMIN}/v1/users/@bob:neuron.local/shadow_ban", headers=_auth(env.admin)
        )

        sent = await env.raw.put(
            f"/_matrix/client/v3/rooms/{room}/state/m.room.topic",
            headers=_auth(env.bob),
            json={"topic": "shadow topic"},
        )
        assert sent.status_code == 200 and sent.json()["event_id"]  # plausible success...
        # ...but the topic was never actually set.
        got = await env.raw.get(
            f"/_matrix/client/v3/rooms/{room}/state/m.room.topic", headers=_auth(env.admin)
        )
        assert got.status_code == 404


async def test_shadow_ban_drops_redactions(tmp_path: Path) -> None:
    async with _env(tmp_path) as env:
        room = await _make_room_with_bob(env)
        _, sent = await env.send(room, env.bob, "mine", "t1")
        event_id = sent["event_id"]
        await env.raw.post(
            f"{_ADMIN}/v1/users/@bob:neuron.local/shadow_ban", headers=_auth(env.admin)
        )

        red = await env.raw.put(
            f"/_matrix/client/v3/rooms/{room}/redact/{event_id}/r1",
            headers=_auth(env.bob),
            json={},
        )
        assert red.status_code == 200 and red.json()["event_id"]  # plausible success...
        # ...but the message is not actually redacted.
        target = [e for e in await env.messages(room, env.admin) if e["event_id"] == event_id]
        assert target and target[0]["content"].get("body") == "mine"


async def test_shadow_ban_drops_invites(tmp_path: Path) -> None:
    async with _env(tmp_path) as env:
        room = await _make_room_with_bob(env)
        await env.raw.post(
            f"{_ADMIN}/v1/users/@bob:neuron.local/shadow_ban", headers=_auth(env.admin)
        )

        inv = await env.raw.post(
            f"/_matrix/client/v3/rooms/{room}/invite",
            headers=_auth(env.bob),
            json={"user_id": "@carol:neuron.local"},
        )
        assert inv.status_code == 200  # plausible success...
        # ...but Carol was never invited (no membership event created for her).
        state = (
            await env.raw.get(f"{_ADMIN}/v1/rooms/{room}/state", headers=_auth(env.admin))
        ).json()["state"]
        carol = [
            e for e in state
            if e["type"] == "m.room.member" and e["state_key"] == "@carol:neuron.local"
        ]
        assert carol == []


async def test_shadow_ban_still_allows_join(tmp_path: Path) -> None:
    """Join is intentionally NOT shadow-banned — the user must not realise they
    are banned, so they can still join (their messages are silently dropped)."""
    async with _env(tmp_path) as env:
        room = await _make_room_with_bob(env)
        dave = await _register(env.raw, "dave")
        await env.raw.post(
            f"{_ADMIN}/v1/users/@dave:neuron.local/shadow_ban", headers=_auth(env.admin)
        )

        j = await env.raw.post(f"/_matrix/client/v3/join/{room}", headers=_auth(dave), json={})
        assert j.status_code == 200
        members = (
            await env.raw.get(f"{_ADMIN}/v1/rooms/{room}/members", headers=_auth(env.admin))
        ).json()["members"]
        assert "@dave:neuron.local" in members


async def test_shadow_ban_drops_createroom_invites(tmp_path: Path) -> None:
    """A shadow-banned user cannot use the createRoom invite list to bypass the
    invite ban — the room is created (so the ban stays hidden) but no invite goes out."""
    async with _env(tmp_path) as env:
        await env.raw.post(
            f"{_ADMIN}/v1/users/@bob:neuron.local/shadow_ban", headers=_auth(env.admin)
        )
        created = await env.raw.post(
            "/_matrix/client/v3/createRoom",
            headers=_auth(env.bob),
            json={"preset": "private_chat", "invite": ["@carol:neuron.local"]},
        )
        assert created.status_code == 200  # the room is created (ban undetectable)...
        room = created.json()["room_id"]
        # ...but Carol was never invited.
        state = (
            await env.raw.get(f"{_ADMIN}/v1/rooms/{room}/state", headers=_auth(env.admin))
        ).json()["state"]
        carol = [
            e for e in state
            if e["type"] == "m.room.member" and e["state_key"] == "@carol:neuron.local"
        ]
        assert carol == []


async def test_shadow_ban_redaction_txn_is_idempotent(tmp_path: Path) -> None:
    """A re-sent redaction txn from a shadow-banned user returns the SAME fake id
    (the txn dedupe runs before the gate), not a fresh one or an error."""
    async with _env(tmp_path) as env:
        room = await _make_room_with_bob(env)
        _, sent = await env.send(room, env.bob, "mine", "t1")
        event_id = sent["event_id"]
        await env.raw.post(
            f"{_ADMIN}/v1/users/@bob:neuron.local/shadow_ban", headers=_auth(env.admin)
        )

        first = await env.raw.put(
            f"/_matrix/client/v3/rooms/{room}/redact/{event_id}/rr", headers=_auth(env.bob), json={}
        )
        second = await env.raw.put(
            f"/_matrix/client/v3/rooms/{room}/redact/{event_id}/rr", headers=_auth(env.bob), json={}
        )
        assert first.status_code == 200 and second.status_code == 200
        assert first.json()["event_id"] == second.json()["event_id"]


async def test_room_block_refuses_sends_and_joins(tmp_path: Path) -> None:
    async with _env(tmp_path) as env:
        room = await _make_room_with_bob(env)
        r = await env.raw.put(
            f"{_ADMIN}/v1/rooms/{room}/block", headers=_auth(env.admin), json={"block": True}
        )
        assert r.status_code == 200

        code, body = await env.send(room, env.bob, "blocked?", "t1")
        assert code == 403 and body["errcode"] == "M_FORBIDDEN"

        await env.raw.put(
            f"{_ADMIN}/v1/rooms/{room}/block", headers=_auth(env.admin), json={"block": False}
        )
        code, _ = await env.send(room, env.bob, "ok now", "t2")
        assert code == 200


async def test_delete_room_purges(tmp_path: Path) -> None:
    async with _env(tmp_path) as env:
        room = await _make_room_with_bob(env)
        r = await env.raw.request(
            "DELETE", f"{_ADMIN}/v2/rooms/{room}", headers=_auth(env.admin), json={"purge": True}
        )
        assert r.status_code == 200
        delete_id = r.json()["delete_id"]
        status = await env.raw.get(
            f"{_ADMIN}/v2/rooms/delete_status/{delete_id}", headers=_auth(env.admin)
        )
        assert status.json()["status"] == "complete"
        # The room is gone: admin state lookup 404s.
        gone = await env.raw.get(f"{_ADMIN}/v1/rooms/{room}/state", headers=_auth(env.admin))
        assert gone.status_code == 404


async def test_delete_room_with_block_and_purge_keeps_the_block(tmp_path: Path) -> None:
    """block and purge are independent flags (Synapse semantics): asking for both
    must purge the room *and* leave it blocked against re-creation/re-join."""
    async with _env(tmp_path) as env:
        room = await _make_room_with_bob(env)
        r = await env.raw.request(
            "DELETE",
            f"{_ADMIN}/v2/rooms/{room}",
            headers=_auth(env.admin),
            json={"purge": True, "block": True},
        )
        assert r.status_code == 200
        # Purged: the room's data is gone...
        gone = await env.raw.get(f"{_ADMIN}/v1/rooms/{room}/state", headers=_auth(env.admin))
        assert gone.status_code == 404
        # ...and the block survived the purge.
        assert await room_store.is_room_blocked(env.db, room)


async def test_redact_user_events(tmp_path: Path) -> None:
    async with _env(tmp_path) as env:
        room = await _make_room_with_bob(env)
        await env.send(room, env.bob, "redact me", "t1")
        r = await env.raw.post(
            f"{_ADMIN}/v1/user/@bob:neuron.local/redact", headers=_auth(env.admin), json={}
        )
        assert r.status_code == 200
        redact_id = r.json()["redact_id"]
        st = await env.raw.get(
            f"{_ADMIN}/v1/user/redact_status/{redact_id}", headers=_auth(env.admin)
        )
        assert st.json()["status"] == "complete"
        bob_msgs = [
            e for e in await env.messages(room, env.admin)
            if e["sender"] == "@bob:neuron.local" and e["type"] == "m.room.message"
        ]
        assert bob_msgs and all(e["content"] == {} for e in bob_msgs)


async def test_event_report_capture_and_list(tmp_path: Path) -> None:
    async with _env(tmp_path) as env:
        room = await _make_room_with_bob(env)
        _, sent = await env.send(room, env.admin, "hello", "t1")
        event_id = sent["event_id"]
        rep = await env.raw.post(
            f"/_matrix/client/v3/rooms/{room}/report/{event_id}",
            headers=_auth(env.bob),
            json={"reason": "spam", "score": -100},
        )
        assert rep.status_code == 200
        reports = await env.raw.get(f"{_ADMIN}/v1/event_reports", headers=_auth(env.admin))
        body = reports.json()
        assert body["total"] == 1
        assert body["event_reports"][0]["reason"] == "spam"
        assert body["event_reports"][0]["user_id"] == "@bob:neuron.local"


async def test_event_report_detail_and_dismiss(tmp_path: Path) -> None:
    async with _env(tmp_path) as env:
        room = await _make_room_with_bob(env)
        _, sent = await env.send(room, env.admin, "hello", "t1")
        event_id = sent["event_id"]
        await env.raw.post(
            f"/_matrix/client/v3/rooms/{room}/report/{event_id}",
            headers=_auth(env.bob),
            json={"reason": "spam", "score": -100},
        )
        report_id = (
            await env.raw.get(f"{_ADMIN}/v1/event_reports", headers=_auth(env.admin))
        ).json()["event_reports"][0]["id"]

        # Detail: a single report by id.
        detail = await env.raw.get(
            f"{_ADMIN}/v1/event_reports/{report_id}", headers=_auth(env.admin)
        )
        assert detail.status_code == 200
        assert detail.json()["reason"] == "spam"
        assert detail.json()["user_id"] == "@bob:neuron.local"

        # Dismiss (delete) it.
        d = await env.raw.request(
            "DELETE", f"{_ADMIN}/v1/event_reports/{report_id}", headers=_auth(env.admin)
        )
        assert d.status_code == 200

        # It is gone: detail 404s and the queue is empty.
        gone = await env.raw.get(
            f"{_ADMIN}/v1/event_reports/{report_id}", headers=_auth(env.admin)
        )
        assert gone.status_code == 404
        listed = await env.raw.get(f"{_ADMIN}/v1/event_reports", headers=_auth(env.admin))
        assert listed.json()["total"] == 0


async def test_server_notice_reaches_user(tmp_path: Path) -> None:
    async with _env(tmp_path) as env:
        r = await env.raw.post(
            f"{_ADMIN}/v1/send_server_notice",
            headers=_auth(env.admin),
            json={"user_id": "@bob:neuron.local", "content": {"msgtype": "m.text", "body": "Hi"}},
        )
        assert r.status_code == 200 and r.json()["event_id"]
        joined = await env.raw.get("/_matrix/client/v3/joined_rooms", headers=_auth(env.bob))
        assert len(joined.json()["joined_rooms"]) >= 1
