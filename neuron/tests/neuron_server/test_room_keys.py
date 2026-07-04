# SPDX-License-Identifier: Apache-2.0
"""Tests for server-side key backup (``/room_keys``): version lifecycle, the
wrong-version write rejection that makes clients rotate, the spec's session
replacement algorithm, and count/etag semantics."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_REG = "/_matrix/client/v3/register"
_B = "/_matrix/client/v3"
_ALGO = "m.megolm_backup.v1.curve25519-aes-sha2"


def _client(tmp_path: Path) -> TestClient:
    settings = NeuronServerSettings(
        name="neuron.local", database_url=f"sqlite:///{tmp_path / 'hs.db'}"
    )
    return TestClient(create_app(settings))


def _register(client: TestClient, username: str) -> str:
    challenge = client.post(_REG, json={"username": username, "password": "pw-123456"})
    session = challenge.json()["session"]
    out = client.post(
        _REG,
        json={
            "username": username,
            "password": "pw-123456",
            "auth": {"type": "m.login.dummy", "session": session},
        },
    ).json()
    return str(out["access_token"])


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_version(client: TestClient, token: str, public_key: str = "pk") -> str:
    resp = client.post(
        f"{_B}/room_keys/version",
        headers=_h(token),
        json={"algorithm": _ALGO, "auth_data": {"public_key": public_key, "signatures": {}}},
    )
    assert resp.status_code == 200
    return str(resp.json()["version"])


def _key(
    first_message_index: int = 0,
    forwarded_count: int = 0,
    is_verified: bool = False,
    payload: str = "ciphertext",
) -> dict[str, Any]:
    return {
        "first_message_index": first_message_index,
        "forwarded_count": forwarded_count,
        "is_verified": is_verified,
        "session_data": {"ephemeral": "e", "ciphertext": payload, "mac": "m"},
    }


# --- version lifecycle -------------------------------------------------------


def test_version_lifecycle(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token = _register(client, "alice")

        # No backup yet: the current-version endpoint 404s.
        resp = client.get(f"{_B}/room_keys/version", headers=_h(token))
        assert resp.status_code == 404 and resp.json()["errcode"] == "M_NOT_FOUND"

        assert _create_version(client, token) == "1"
        current = client.get(f"{_B}/room_keys/version", headers=_h(token)).json()
        assert current["version"] == "1"
        assert current["algorithm"] == _ALGO
        assert current["auth_data"]["public_key"] == "pk"
        assert current["count"] == 0
        assert isinstance(current["etag"], str)

        # A second version becomes current; the first stays fetchable by number.
        assert _create_version(client, token, public_key="pk2") == "2"
        assert client.get(f"{_B}/room_keys/version", headers=_h(token)).json()["version"] == "2"
        v1 = client.get(f"{_B}/room_keys/version/1", headers=_h(token)).json()
        assert v1["version"] == "1" and v1["auth_data"]["public_key"] == "pk"

        # Unknown / malformed version numbers 404.
        assert client.get(f"{_B}/room_keys/version/9", headers=_h(token)).status_code == 404
        assert client.get(f"{_B}/room_keys/version/bogus", headers=_h(token)).status_code == 404


def test_version_update_auth_data(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token = _register(client, "alice")
        version = _create_version(client, token)

        ok = client.put(
            f"{_B}/room_keys/version/{version}",
            headers=_h(token),
            json={
                "algorithm": _ALGO,
                "auth_data": {"public_key": "rotated", "signatures": {}},
                "version": version,
            },
        )
        assert ok.status_code == 200
        got = client.get(f"{_B}/room_keys/version", headers=_h(token)).json()
        assert got["auth_data"]["public_key"] == "rotated"

        # The algorithm must match the existing backup.
        bad_algo = client.put(
            f"{_B}/room_keys/version/{version}",
            headers=_h(token),
            json={"algorithm": "m.other.algorithm", "auth_data": {"public_key": "x"}},
        )
        assert bad_algo.status_code == 400 and bad_algo.json()["errcode"] == "M_INVALID_PARAM"

        # A version in the body, if given, must match the path.
        bad_version = client.put(
            f"{_B}/room_keys/version/{version}",
            headers=_h(token),
            json={"algorithm": _ALGO, "auth_data": {"public_key": "x"}, "version": "999"},
        )
        assert bad_version.status_code == 400

        # Updating a nonexistent version 404s.
        missing = client.put(
            f"{_B}/room_keys/version/42",
            headers=_h(token),
            json={"algorithm": _ALGO, "auth_data": {"public_key": "x"}},
        )
        assert missing.status_code == 404


def test_version_delete_is_soft_and_numbers_stay_monotonic(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token = _register(client, "alice")
        _create_version(client, token)  # v1
        _create_version(client, token)  # v2

        assert client.delete(f"{_B}/room_keys/version/2", headers=_h(token)).status_code == 200
        # The previous version becomes current again; the deleted one 404s.
        assert client.get(f"{_B}/room_keys/version", headers=_h(token)).json()["version"] == "1"
        assert client.get(f"{_B}/room_keys/version/2", headers=_h(token)).status_code == 404
        assert client.delete(f"{_B}/room_keys/version/2", headers=_h(token)).status_code == 404

        # A deleted version's number is never reused.
        assert _create_version(client, token) == "3"

        # Writes to the deleted version fail (403: v3 is now current).
        write = client.put(
            f"{_B}/room_keys/keys/!r:neuron.local/s1?version=2",
            headers=_h(token),
            json=_key(),
        )
        assert write.status_code == 403
        assert write.json()["errcode"] == "M_WRONG_ROOM_KEYS_VERSION"
        assert write.json()["current_version"] == "3"


def test_room_keys_require_auth(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert client.get(f"{_B}/room_keys/version").status_code == 401
        assert client.put(f"{_B}/room_keys/keys?version=1", json={"rooms": {}}).status_code == 401


# --- wrong-version writes ----------------------------------------------------


def test_writes_to_non_current_version_are_rejected(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token = _register(client, "alice")

        # With no backup at all, writes 404.
        none = client.put(
            f"{_B}/room_keys/keys?version=1", headers=_h(token), json={"rooms": {}}
        )
        assert none.status_code == 404 and none.json()["errcode"] == "M_NOT_FOUND"

        _create_version(client, token)  # v1
        _create_version(client, token)  # v2 (current)

        for method, url, body in (
            ("PUT", f"{_B}/room_keys/keys?version=1", {"rooms": {}}),
            ("PUT", f"{_B}/room_keys/keys/!r:neuron.local?version=1", {"sessions": {}}),
            ("PUT", f"{_B}/room_keys/keys/!r:neuron.local/s1?version=1", _key()),
            ("DELETE", f"{_B}/room_keys/keys?version=1", None),
            ("DELETE", f"{_B}/room_keys/keys/!r:neuron.local?version=1", None),
            ("DELETE", f"{_B}/room_keys/keys/!r:neuron.local/s1?version=1", None),
        ):
            resp = client.request(method, url, headers=_h(token), json=body)
            assert resp.status_code == 403, (method, url)
            payload = resp.json()
            assert payload["errcode"] == "M_WRONG_ROOM_KEYS_VERSION"
            assert payload["current_version"] == "2"

        # The version query parameter is required.
        missing = client.put(f"{_B}/room_keys/keys", headers=_h(token), json={"rooms": {}})
        assert missing.status_code == 400


# --- replacement algorithm ---------------------------------------------------


def test_replacement_algorithm(tmp_path: Path) -> None:
    room, session = "!r:neuron.local", "s1"
    with _client(tmp_path) as client:
        token = _register(client, "alice")
        version = _create_version(client, token)
        url = f"{_B}/room_keys/keys/{room}/{session}?version={version}"

        def put(key: dict[str, Any]) -> dict[str, Any]:
            resp = client.put(url, headers=_h(token), json=key)
            assert resp.status_code == 200
            return dict(resp.json())

        def stored() -> dict[str, Any]:
            return dict(client.get(url, headers=_h(token)).json())

        first = put(_key(first_message_index=5, forwarded_count=2, payload="original"))
        assert first["count"] == 1

        # Higher first_message_index at equal verification: kept, not replaced.
        rejected = put(_key(first_message_index=6, forwarded_count=9, payload="worse"))
        assert stored()["session_data"]["ciphertext"] == "original"
        assert rejected["etag"] == first["etag"]  # nothing changed

        # Same index, higher forwarded_count: replaced.
        put(_key(first_message_index=5, forwarded_count=3, payload="more-forwards"))
        assert stored()["session_data"]["ciphertext"] == "more-forwards"

        # Lower first_message_index beats a higher forwarded_count.
        put(_key(first_message_index=3, forwarded_count=0, payload="earlier"))
        assert stored()["session_data"]["ciphertext"] == "earlier"

        # is_verified trumps both other fields.
        put(_key(first_message_index=10, forwarded_count=0, is_verified=True, payload="verified"))
        assert stored()["session_data"]["ciphertext"] == "verified"

        # And an unverified key never replaces a verified one.
        put(_key(first_message_index=0, forwarded_count=99, payload="unverified"))
        assert stored()["session_data"]["ciphertext"] == "verified"

        # Identical metadata does not replace either (the stored blob is kept).
        put(_key(first_message_index=10, forwarded_count=0, is_verified=True, payload="other"))
        assert stored()["session_data"]["ciphertext"] == "verified"


# --- count/etag semantics ----------------------------------------------------


def test_count_and_etag_track_actual_changes(tmp_path: Path) -> None:
    room = "!r:neuron.local"
    with _client(tmp_path) as client:
        token = _register(client, "alice")
        version = _create_version(client, token)
        q = f"?version={version}"

        one = client.put(
            f"{_B}/room_keys/keys/{room}/s1{q}", headers=_h(token), json=_key()
        ).json()
        assert one["count"] == 1

        two = client.put(
            f"{_B}/room_keys/keys/{room}/s2{q}", headers=_h(token), json=_key()
        ).json()
        assert two["count"] == 2 and two["etag"] != one["etag"]

        # The version endpoints report the same count/etag.
        info = client.get(f"{_B}/room_keys/version", headers=_h(token)).json()
        assert info["count"] == 2 and info["etag"] == two["etag"]

        # A rejected (not-better) upload changes neither count nor etag.
        same = client.put(
            f"{_B}/room_keys/keys/{room}/s1{q}", headers=_h(token), json=_key(payload="dup")
        ).json()
        assert same["count"] == 2 and same["etag"] == two["etag"]

        # Deleting a session bumps the etag and drops the count.
        gone = client.delete(f"{_B}/room_keys/keys/{room}/s1{q}", headers=_h(token)).json()
        assert gone["count"] == 1 and gone["etag"] != two["etag"]

        # Deleting nothing leaves the etag alone.
        noop = client.delete(f"{_B}/room_keys/keys/{room}/s1{q}", headers=_h(token)).json()
        assert noop["count"] == 1 and noop["etag"] == gone["etag"]

        # Each version has its own keys and etag: a fresh version starts empty.
        v2 = _create_version(client, token)
        fresh = client.get(f"{_B}/room_keys/version/{v2}", headers=_h(token)).json()
        assert fresh["count"] == 0
        old = client.get(f"{_B}/room_keys/version/{version}", headers=_h(token)).json()
        assert old["count"] == 1


# --- bulk / per-room / per-session round-trips --------------------------------


def test_bulk_room_and_session_roundtrips(tmp_path: Path) -> None:
    r1, r2 = "!one:neuron.local", "!two:neuron.local"
    with _client(tmp_path) as client:
        token = _register(client, "alice")
        version = _create_version(client, token)
        q = f"?version={version}"

        # Nothing stored yet: bulk GET returns an empty rooms object; narrower
        # GETs 404.
        assert client.get(f"{_B}/room_keys/keys{q}", headers=_h(token)).json() == {"rooms": {}}
        assert client.get(f"{_B}/room_keys/keys/{r1}{q}", headers=_h(token)).status_code == 404
        assert (
            client.get(f"{_B}/room_keys/keys/{r1}/s1{q}", headers=_h(token)).status_code == 404
        )

        bulk = client.put(
            f"{_B}/room_keys/keys{q}",
            headers=_h(token),
            json={
                "rooms": {
                    r1: {"sessions": {"s1": _key(payload="r1s1"), "s2": _key(payload="r1s2")}},
                    r2: {"sessions": {"s3": _key(payload="r2s3")}},
                }
            },
        )
        assert bulk.status_code == 200 and bulk.json()["count"] == 3

        # Per-room PUT merges more sessions into the same room.
        merged = client.put(
            f"{_B}/room_keys/keys/{r2}{q}",
            headers=_h(token),
            json={"sessions": {"s4": _key(payload="r2s4")}},
        ).json()
        assert merged["count"] == 4

        everything = client.get(f"{_B}/room_keys/keys{q}", headers=_h(token)).json()
        assert set(everything["rooms"]) == {r1, r2}
        assert set(everything["rooms"][r2]["sessions"]) == {"s3", "s4"}

        room_one = client.get(f"{_B}/room_keys/keys/{r1}{q}", headers=_h(token)).json()
        assert set(room_one["sessions"]) == {"s1", "s2"}

        session = client.get(f"{_B}/room_keys/keys/{r1}/s1{q}", headers=_h(token)).json()
        assert session["session_data"]["ciphertext"] == "r1s1"
        assert session["first_message_index"] == 0
        assert session["forwarded_count"] == 0
        assert session["is_verified"] is False

        # Per-session delete removes just that session.
        client.delete(f"{_B}/room_keys/keys/{r1}/s1{q}", headers=_h(token))
        assert (
            client.get(f"{_B}/room_keys/keys/{r1}/s1{q}", headers=_h(token)).status_code == 404
        )
        assert set(
            client.get(f"{_B}/room_keys/keys/{r1}{q}", headers=_h(token)).json()["sessions"]
        ) == {"s2"}

        # Per-room delete removes the room; the other room survives.
        assert (
            client.delete(f"{_B}/room_keys/keys/{r1}{q}", headers=_h(token)).json()["count"] == 2
        )
        assert client.get(f"{_B}/room_keys/keys/{r1}{q}", headers=_h(token)).status_code == 404

        # Bulk delete clears the version entirely.
        assert client.delete(f"{_B}/room_keys/keys{q}", headers=_h(token)).json()["count"] == 0
        assert client.get(f"{_B}/room_keys/keys{q}", headers=_h(token)).json() == {"rooms": {}}


def test_backups_are_per_user(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        bob = _register(client, "bob")
        _create_version(client, alice)
        # Bob has no backup even though Alice does.
        assert client.get(f"{_B}/room_keys/version", headers=_h(bob)).status_code == 404
        # Bob's first version is also "1" — numbering is per user.
        assert _create_version(client, bob) == "1"


def test_malformed_key_data_is_rejected(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token = _register(client, "alice")
        version = _create_version(client, token)
        url = f"{_B}/room_keys/keys/!r:neuron.local/s1?version={version}"
        bad = client.put(
            url, headers=_h(token), json={"first_message_index": "zero", "is_verified": False}
        )
        assert bad.status_code == 400 and bad.json()["errcode"] == "M_BAD_JSON"

        no_body = client.post(f"{_B}/room_keys/version", headers=_h(token), json={})
        assert no_body.status_code == 400
