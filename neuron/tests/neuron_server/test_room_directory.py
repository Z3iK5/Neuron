# SPDX-License-Identifier: Apache-2.0
"""Tests for room aliases and the public room directory (CS + federation).

Single-server behaviour uses the sync ``TestClient``; the federation query and the
join-by-remote-alias path use two in-process apps wired together over an ASGI
transport (the same two-server seam as ``test_federation_*``).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_CS = "/_matrix/client/v3"
_QUERY = "/_matrix/federation/v1/query/directory"


# --- single-server helpers -------------------------------------------------


def _client(tmp_path: Path, name: str = "neuron.local") -> TestClient:
    settings = NeuronServerSettings(
        name=name, database_url=f"sqlite:///{tmp_path / 'hs.db'}"
    )
    return TestClient(create_app(settings))


def _register(client: TestClient, username: str) -> tuple[str, str]:
    session = client.post(
        f"{_CS}/register", json={"username": username, "password": "pw-123456"}
    ).json()["session"]
    out = client.post(
        f"{_CS}/register",
        json={
            "username": username,
            "password": "pw-123456",
            "auth": {"type": "m.login.dummy", "session": session},
        },
    ).json()
    return out["access_token"], out["user_id"]


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_room(client: TestClient, token: str, **body: object) -> str:
    resp = client.post(f"{_CS}/createRoom", headers=_h(token), json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["room_id"]


def _alias_path(alias: str) -> str:
    return f"{_CS}/directory/room/{quote(alias, safe='')}"


# --- aliases ---------------------------------------------------------------


def test_alias_create_resolve_delete(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, _ = _register(client, "alice")
        room_id = _create_room(client, token)
        alias = "#general:neuron.local"

        put = client.put(_alias_path(alias), headers=_h(token), json={"room_id": room_id})
        assert put.status_code == 200, put.text

        got = client.get(_alias_path(alias))
        assert got.status_code == 200
        assert got.json()["room_id"] == room_id
        assert "neuron.local" in got.json()["servers"]

        deleted = client.delete(_alias_path(alias), headers=_h(token))
        assert deleted.status_code == 200
        assert client.get(_alias_path(alias)).status_code == 404


def test_alias_duplicate_is_409(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, _ = _register(client, "alice")
        room_id = _create_room(client, token)
        alias = "#dup:neuron.local"
        assert client.put(
            _alias_path(alias), headers=_h(token), json={"room_id": room_id}
        ).status_code == 200
        again = client.put(_alias_path(alias), headers=_h(token), json={"room_id": room_id})
        assert again.status_code == 409
        assert again.json()["errcode"] == "M_UNKNOWN"


def test_alias_wrong_server_is_400(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, _ = _register(client, "alice")
        room_id = _create_room(client, token)
        alias = "#elsewhere:other.example"
        resp = client.put(_alias_path(alias), headers=_h(token), json={"room_id": room_id})
        assert resp.status_code == 400
        assert resp.json()["errcode"] == "M_INVALID_PARAM"


def test_non_admin_cannot_delete_alias(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice, _ = _register(client, "alice")  # room admin (creator, PL 100)
        bob, _ = _register(client, "bob")  # alias creator, no room power
        carol, _ = _register(client, "carol")  # neither
        room_id = _create_room(client, alice)
        alias = "#shared:neuron.local"
        assert client.put(
            _alias_path(alias), headers=_h(bob), json={"room_id": room_id}
        ).status_code == 200

        # A third party who is neither the alias creator nor a room admin: 403.
        forbidden = client.delete(_alias_path(alias), headers=_h(carol))
        assert forbidden.status_code == 403

        # The room admin may delete an alias they did not create.
        assert client.delete(_alias_path(alias), headers=_h(alice)).status_code == 200


def test_create_room_with_alias_sets_canonical_alias(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, _ = _register(client, "alice")
        room_id = _create_room(client, token, room_alias_name="lounge", visibility="public")
        alias = "#lounge:neuron.local"

        # The alias resolves,
        assert client.get(_alias_path(alias)).json()["room_id"] == room_id
        # and the canonical alias state event is set.
        canonical = client.get(
            f"{_CS}/rooms/{room_id}/state/m.room.canonical_alias", headers=_h(token)
        )
        assert canonical.status_code == 200
        assert canonical.json()["alias"] == alias


def test_create_room_duplicate_alias_name_is_409(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, _ = _register(client, "alice")
        _create_room(client, token, room_alias_name="taken")
        resp = client.post(
            f"{_CS}/createRoom", headers=_h(token), json={"room_alias_name": "taken"}
        )
        assert resp.status_code == 409
        assert resp.json()["errcode"] == "M_ROOM_IN_USE"


# --- public directory ------------------------------------------------------


def test_visibility_controls_public_listing(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, _ = _register(client, "alice")
        public = _create_room(client, token, visibility="public", name="Public Room")
        private = _create_room(client, token, visibility="private", name="Secret Room")

        chunk = client.get(f"{_CS}/publicRooms").json()["chunk"]
        ids = {c["room_id"] for c in chunk}
        assert public in ids
        assert private not in ids


def test_public_rooms_chunk_shape(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, _ = _register(client, "alice")
        room_id = _create_room(
            client, token, visibility="public", name="Chatter", topic="all things",
            room_alias_name="chatter",
        )
        body = client.get(f"{_CS}/publicRooms").json()
        assert body["total_room_count_estimate"] >= 1
        entry = next(c for c in body["chunk"] if c["room_id"] == room_id)
        assert entry["name"] == "Chatter"
        assert entry["topic"] == "all things"
        assert entry["canonical_alias"] == "#chatter:neuron.local"
        assert entry["num_joined_members"] == 1
        assert entry["join_rule"] == "public"
        assert entry["world_readable"] is False
        assert entry["guest_can_join"] is False


def test_public_rooms_search_term(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, _ = _register(client, "alice")
        alpha = _create_room(client, token, visibility="public", name="Alpha Squad")
        beta = _create_room(client, token, visibility="public", name="Beta Team")

        resp = client.post(
            f"{_CS}/publicRooms", json={"filter": {"generic_search_term": "alpha"}}
        )
        ids = {c["room_id"] for c in resp.json()["chunk"]}
        assert alpha in ids
        assert beta not in ids


def test_public_rooms_pagination(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, _ = _register(client, "alice")
        for i in range(3):
            _create_room(client, token, visibility="public", name=f"Room {i}")

        first = client.post(f"{_CS}/publicRooms", json={"limit": 2}).json()
        assert len(first["chunk"]) == 2
        assert first["total_room_count_estimate"] == 3
        assert "next_batch" in first

        second = client.post(
            f"{_CS}/publicRooms", json={"limit": 2, "since": first["next_batch"]}
        ).json()
        assert len(second["chunk"]) == 1
        assert "prev_batch" in second


def test_directory_list_visibility_flag(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice, _ = _register(client, "alice")
        bob, _ = _register(client, "bob")
        room_id = _create_room(client, alice)

        assert client.get(
            f"{_CS}/directory/list/room/{room_id}", headers=_h(alice)
        ).json()["visibility"] == "private"

        # A non-admin cannot publish the room.
        forbidden = client.put(
            f"{_CS}/directory/list/room/{room_id}",
            headers=_h(bob),
            json={"visibility": "public"},
        )
        assert forbidden.status_code == 403

        assert client.put(
            f"{_CS}/directory/list/room/{room_id}",
            headers=_h(alice),
            json={"visibility": "public"},
        ).status_code == 200
        assert client.get(
            f"{_CS}/directory/list/room/{room_id}", headers=_h(alice)
        ).json()["visibility"] == "public"

        listed = {c["room_id"] for c in client.get(f"{_CS}/publicRooms").json()["chunk"]}
        assert room_id in listed


def test_join_by_local_alias(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice, _ = _register(client, "alice")
        bob, bob_id = _register(client, "bob")
        room_id = _create_room(
            client, alice, preset="public_chat", room_alias_name="open"
        )
        joined = client.post(f"{_CS}/join/{quote('#open:neuron.local', safe='')}", headers=_h(bob))
        assert joined.status_code == 200, joined.text
        assert joined.json()["room_id"] == room_id
        members = client.get(
            f"{_CS}/rooms/{room_id}/joined_members", headers=_h(alice)
        ).json()["joined"]
        assert bob_id in members


# --- two-server federation -------------------------------------------------


def _opener(apps: dict[str, FastAPI]):  # noqa: ANN202 - test helper
    def open_client(server_name: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=apps[server_name]),
            base_url=f"https://{server_name}",
        )

    return open_client


async def _register_async(client: httpx.AsyncClient, username: str) -> dict[str, str]:
    session = (
        await client.post(
            f"{_CS}/register", json={"username": username, "password": "pw-123456"}
        )
    ).json()["session"]
    out = await client.post(
        f"{_CS}/register",
        json={
            "username": username,
            "password": "pw-123456",
            "auth": {"type": "m.login.dummy", "session": session},
        },
    )
    return {"Authorization": f"Bearer {out.json()['access_token']}"}


@pytest.fixture
def two_apps(tmp_path: Path) -> dict[str, FastAPI]:
    return {
        name: create_app(
            NeuronServerSettings(
                name=name, database_url=f"sqlite:///{tmp_path / f'{name}.db'}"
            )
        )
        for name in ("a.test", "b.test")
    }


async def test_federation_query_directory_and_remote_join(two_apps: dict[str, FastAPI]) -> None:
    app_a, app_b = two_apps["a.test"], two_apps["b.test"]
    async with app_a.router.lifespan_context(app_a), app_b.router.lifespan_context(app_b):
        app_a.state.federation_client.open_client = _opener(two_apps)
        app_b.state.federation_client.open_client = _opener(two_apps)

        client_a = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_a), base_url="https://a.test"
        )
        client_b = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_b), base_url="https://b.test"
        )
        try:
            bob_h = await _register_async(client_b, "bob")
            room_id = (
                await client_b.post(
                    f"{_CS}/createRoom",
                    headers=bob_h,
                    json={"preset": "public_chat", "room_alias_name": "chat"},
                )
            ).json()["room_id"]
            alias = "#chat:b.test"

            # Inbound federation query resolves B's alias (signed request from A).
            resolved = await app_a.state.federation_client.get_json(
                "b.test", f"{_QUERY}?room_alias={quote(alias, safe='')}"
            )
            assert resolved["room_id"] == room_id
            assert "b.test" in resolved["servers"]

            # An unsigned inbound query is rejected.
            raw = await client_b.get(f"{_QUERY}?room_alias={quote(alias, safe='')}")
            assert raw.status_code == 401

            # A non-local alias is M_NOT_FOUND on B.
            with pytest.raises(httpx.HTTPStatusError) as err:
                await app_a.state.federation_client.get_json(
                    "b.test", f"{_QUERY}?room_alias={quote('#chat:a.test', safe='')}"
                )
            assert err.value.response.status_code == 404

            # Outbound: a user on A joins B's room by its remote alias.
            alice_h = await _register_async(client_a, "alice")
            joined = await client_a.post(
                f"{_CS}/join/{quote(alias, safe='')}", headers=alice_h
            )
            assert joined.status_code == 200, joined.text
            assert joined.json()["room_id"] == room_id

            from neuron_server.storage import rooms as store

            members = await store.get_joined_members(app_b.state.db, room_id)
            assert "@alice:a.test" in members
        finally:
            await client_a.aclose()
            await client_b.aclose()
