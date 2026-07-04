# SPDX-License-Identifier: Apache-2.0
"""Tests for push rules: server defaults, custom-rule CRUD, enabled/actions
overrides on predefined rules, and the m.push_rules account-data bump."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_REG = "/_matrix/client/v3/register"
_B = "/_matrix/client/v3"


def _client(tmp_path: Path) -> TestClient:
    settings = NeuronServerSettings(
        name="neuron.local", database_url=f"sqlite:///{tmp_path / 'hs.db'}"
    )
    return TestClient(create_app(settings))


def _register(client: TestClient, username: str) -> tuple[str, str]:
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
    return out["access_token"], out["user_id"]


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


_OVERRIDE_IDS = [
    ".m.rule.master",
    ".m.rule.suppress_notices",
    ".m.rule.invite_for_me",
    ".m.rule.member_event",
    ".m.rule.contains_display_name",
    ".m.rule.roomnotif",
    ".m.rule.tombstone",
]
_UNDERRIDE_IDS = [
    ".m.rule.call",
    ".m.rule.encrypted_room_one_to_one",
    ".m.rule.room_one_to_one",
    ".m.rule.message",
    ".m.rule.encrypted",
]


def test_fresh_user_gets_server_default_ruleset(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, user_id = _register(client, "alice")
        body = client.get(f"{_B}/pushrules/", headers=_h(token)).json()
        rules = body["global"]
        assert [r["rule_id"] for r in rules["override"]] == _OVERRIDE_IDS
        assert [r["rule_id"] for r in rules["underride"]] == _UNDERRIDE_IDS
        assert rules["room"] == [] and rules["sender"] == []

        by_id = {r["rule_id"]: r for r in rules["override"]}
        master = by_id[".m.rule.master"]
        assert master["default"] is True
        assert master["enabled"] is False
        assert master["actions"] == []
        invite = by_id[".m.rule.invite_for_me"]
        assert invite["enabled"] is True
        assert {"kind": "event_match", "key": "state_key", "pattern": user_id} in invite[
            "conditions"
        ]
        assert "notify" in invite["actions"]
        display = by_id[".m.rule.contains_display_name"]
        assert display["conditions"] == [{"kind": "contains_display_name"}]
        assert {"set_tweak": "highlight"} in display["actions"]

        (content_rule,) = rules["content"]
        assert content_rule["rule_id"] == ".m.rule.contains_user_name"
        assert content_rule["pattern"] == "alice"

        # The scoped endpoint returns the same ruleset without the wrapper.
        scoped = client.get(f"{_B}/pushrules/global", headers=_h(token)).json()
        assert [r["rule_id"] for r in scoped["override"]] == _OVERRIDE_IDS


def test_custom_rule_create_read_delete(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, _user = _register(client, "alice")
        url = f"{_B}/pushrules/global/override/net.example.custom"
        put = client.put(
            url,
            headers=_h(token),
            json={
                "actions": ["notify"],
                "conditions": [{"kind": "event_match", "key": "content.body", "pattern": "cake"}],
            },
        )
        assert put.status_code == 200

        rule = client.get(url, headers=_h(token)).json()
        assert rule["rule_id"] == "net.example.custom"
        assert rule["default"] is False
        assert rule["enabled"] is True
        assert rule["actions"] == ["notify"]

        # Custom rules outrank defaults but sit below .m.rule.master.
        override = client.get(f"{_B}/pushrules/", headers=_h(token)).json()["global"]["override"]
        ids = [r["rule_id"] for r in override]
        assert ids[0] == ".m.rule.master"
        assert ids[1] == "net.example.custom"

        assert client.delete(url, headers=_h(token)).status_code == 200
        assert client.get(url, headers=_h(token)).status_code == 404
        assert client.delete(url, headers=_h(token)).status_code == 404


def test_server_default_rules_cannot_be_deleted_or_replaced(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, _user = _register(client, "alice")
        deleted = client.delete(
            f"{_B}/pushrules/global/override/.m.rule.master", headers=_h(token)
        )
        assert deleted.status_code == 404
        assert deleted.json()["errcode"] == "M_NOT_FOUND"
        replaced = client.put(
            f"{_B}/pushrules/global/override/.m.rule.master",
            headers=_h(token),
            json={"actions": ["notify"]},
        )
        assert replaced.status_code == 400


def test_content_rule_requires_pattern(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, _user = _register(client, "alice")
        missing = client.put(
            f"{_B}/pushrules/global/content/no-pattern",
            headers=_h(token),
            json={"actions": ["notify"]},
        )
        assert missing.status_code == 400
        ok = client.put(
            f"{_B}/pushrules/global/content/cake",
            headers=_h(token),
            json={"actions": ["notify"], "pattern": "cake"},
        )
        assert ok.status_code == 200
        rule = client.get(f"{_B}/pushrules/global/content/cake", headers=_h(token)).json()
        assert rule["pattern"] == "cake"


def test_before_after_ordering(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, _user = _register(client, "alice")

        def put(rule_id: str, query: str = "") -> None:
            resp = client.put(
                f"{_B}/pushrules/global/underride/{rule_id}{query}",
                headers=_h(token),
                json={"actions": ["notify"], "conditions": []},
            )
            assert resp.status_code == 200

        put("aaa")
        put("bbb")
        put("ccc", "?before=bbb")
        put("ddd", "?after=aaa")
        underride = client.get(f"{_B}/pushrules/", headers=_h(token)).json()["global"][
            "underride"
        ]
        ids = [r["rule_id"] for r in underride]
        assert ids[:4] == ["aaa", "ddd", "ccc", "bbb"]
        assert ids[4:] == _UNDERRIDE_IDS

        missing_anchor = client.put(
            f"{_B}/pushrules/global/underride/eee?before=nope",
            headers=_h(token),
            json={"actions": ["notify"], "conditions": []},
        )
        assert missing_anchor.status_code == 400


def test_enabled_and_actions_overrides_on_predefined_rules(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, _user = _register(client, "alice")
        enable = client.put(
            f"{_B}/pushrules/global/override/.m.rule.master/enabled",
            headers=_h(token),
            json={"enabled": True},
        )
        assert enable.status_code == 200
        actions = client.put(
            f"{_B}/pushrules/global/underride/.m.rule.message/actions",
            headers=_h(token),
            json={"actions": ["notify", {"set_tweak": "sound", "value": "default"}]},
        )
        assert actions.status_code == 200

        # Both overrides persist and are merged into the default rules.
        rules = client.get(f"{_B}/pushrules/", headers=_h(token)).json()["global"]
        master = next(r for r in rules["override"] if r["rule_id"] == ".m.rule.master")
        assert master["enabled"] is True
        assert master["default"] is True  # still a server-default rule
        message = next(r for r in rules["underride"] if r["rule_id"] == ".m.rule.message")
        assert message["actions"] == ["notify", {"set_tweak": "sound", "value": "default"}]

        got = client.get(
            f"{_B}/pushrules/global/override/.m.rule.master/enabled", headers=_h(token)
        ).json()
        assert got == {"enabled": True}

        # Unknown rules 404 on the enabled/actions endpoints.
        unknown = client.put(
            f"{_B}/pushrules/global/override/.m.rule.nope/enabled",
            headers=_h(token),
            json={"enabled": True},
        )
        assert unknown.status_code == 404


def test_push_rule_change_surfaces_as_account_data_in_sync(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, user_id = _register(client, "alice")
        since = client.get(f"{_B}/sync", headers=_h(token)).json()["next_batch"]

        assert client.put(
            f"{_B}/pushrules/global/override/.m.rule.master/enabled",
            headers=_h(token),
            json={"enabled": True},
        ).status_code == 200

        body = client.get(f"{_B}/sync?since={since}&timeout=0", headers=_h(token)).json()
        events = body["account_data"]["events"]
        push = next(e for e in events if e["type"] == "m.push_rules")
        ruleset = push["content"]["global"]
        master = next(r for r in ruleset["override"] if r["rule_id"] == ".m.rule.master")
        assert master["enabled"] is True
        assert next(
            r for r in ruleset["content"] if r["rule_id"] == ".m.rule.contains_user_name"
        )["pattern"] == "alice"
        assert user_id.startswith("@alice:")
