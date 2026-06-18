# SPDX-License-Identifier: Apache-2.0
"""Unit tests for state resolution v2 (HS-7 step 6c).

These exercise the algorithm on scenarios whose outcome is unambiguous — either
forced by the auth rules or by the documented tie-breaks — so they validate the
machinery without depending on adversarial corner cases.
"""

from __future__ import annotations

from neuron_server.rooms import state_resolution as sr
from neuron_server.rooms.events import Event

_ALICE = "@alice:hs"
_BOB = "@bob:hs"
_PL_CONTENT = {
    "users": {_ALICE: 100},
    "users_default": 0,
    "state_default": 50,
    "events_default": 0,
    "events": {},
}


def _ev(
    event_id: str,
    etype: str,
    sender: str,
    *,
    state_key: str = "",
    content: dict | None = None,
    auth: tuple[str, ...] = (),
    ts: int = 0,
) -> Event:
    return Event(
        event_id=event_id,
        room_id="!r:hs",
        type=etype,
        sender=sender,
        content=content or {},
        origin_server_ts=ts,
        depth=0,
        stream_ordering=0,
        state_key=state_key,
        auth_events=list(auth),
    )


def _base() -> tuple[dict[str, Event], sr.StateMap]:
    """A small room: create, alice & bob joined, a power-levels event."""
    create = _ev("$c", "m.room.create", _ALICE, content={"room_version": "11"})
    m_alice = _ev("$ma", "m.room.member", _ALICE, state_key=_ALICE,
                  content={"membership": "join"}, auth=("$c",))
    pl = _ev("$pl", "m.room.power_levels", _ALICE, content=_PL_CONTENT, auth=("$c", "$ma"))
    m_bob = _ev("$mb", "m.room.member", _BOB, state_key=_BOB,
                content={"membership": "join"}, auth=("$c", "$pl"))
    event_map = {e.event_id: e for e in (create, m_alice, pl, m_bob)}
    state = {
        ("m.room.create", ""): "$c",
        ("m.room.member", _ALICE): "$ma",
        ("m.room.power_levels", ""): "$pl",
        ("m.room.member", _BOB): "$mb",
    }
    return event_map, state


def test_single_state_map_is_returned_unchanged() -> None:
    _event_map, state = _base()
    assert sr.resolve([state], {}) == state


def test_separate_splits_agree_and_disagree() -> None:
    unconflicted, conflicted = sr.separate(
        [
            {("a", ""): "1", ("b", ""): "2"},
            {("a", ""): "1", ("b", ""): "3", ("c", ""): "4"},
        ]
    )
    assert unconflicted == {("a", ""): "1", ("c", ""): "4"}
    assert conflicted == {("b", ""): {"2", "3"}}


def test_auth_difference() -> None:
    create = _ev("$c", "m.room.create", _ALICE)
    a = _ev("$a", "m.room.topic", _ALICE, auth=("$c",))
    b = _ev("$b", "m.room.topic", _ALICE, auth=("$c",))
    event_map = {"$c": create, "$a": a, "$b": b}
    diff = sr.auth_difference(
        [{("m.room.topic", ""): "$a"}, {("m.room.topic", ""): "$b"}], event_map
    )
    assert diff == {"$a", "$b"}  # $c is in both chains, so excluded


def test_is_power_event() -> None:
    assert sr.is_power_event(_ev("$1", "m.room.power_levels", _ALICE))
    assert sr.is_power_event(_ev("$2", "m.room.join_rules", _ALICE))
    # A ban of someone else is a power event; leaving yourself is not.
    assert sr.is_power_event(
        _ev("$3", "m.room.member", _ALICE, state_key=_BOB, content={"membership": "ban"})
    )
    assert not sr.is_power_event(
        _ev("$4", "m.room.member", _BOB, state_key=_BOB, content={"membership": "leave"})
    )
    assert not sr.is_power_event(_ev("$5", "m.room.message", _ALICE, state_key=None))


def test_conflict_between_authorized_events_breaks_by_timestamp() -> None:
    event_map, base = _base()
    topic_a = _ev("$ta", "m.room.topic", _ALICE, content={"topic": "A"},
                  auth=("$c", "$ma", "$pl"), ts=100)
    topic_b = _ev("$tb", "m.room.topic", _ALICE, content={"topic": "B"},
                  auth=("$c", "$ma", "$pl"), ts=200)
    event_map[topic_a.event_id] = topic_a
    event_map[topic_b.event_id] = topic_b
    s1 = {**base, ("m.room.topic", ""): "$ta"}
    s2 = {**base, ("m.room.topic", ""): "$tb"}

    resolved = sr.resolve([s1, s2], event_map)
    # Both are authorized, so the later (mainline tie-break by timestamp) wins.
    assert resolved[("m.room.topic", "")] == "$tb"
    # Unconflicted keys survive untouched.
    assert resolved[("m.room.power_levels", "")] == "$pl"


def test_auth_rules_override_ordering() -> None:
    event_map, base = _base()
    # Alice (PL 100) may set the topic; Bob (PL 0) may not, despite a later ts.
    topic_alice = _ev("$ta", "m.room.topic", _ALICE, content={"topic": "ok"},
                      auth=("$c", "$ma", "$pl"), ts=100)
    topic_bob = _ev("$tb", "m.room.topic", _BOB, content={"topic": "nope"},
                    auth=("$c", "$mb", "$pl"), ts=200)
    event_map[topic_alice.event_id] = topic_alice
    event_map[topic_bob.event_id] = topic_bob
    s1 = {**base, ("m.room.topic", ""): "$ta"}
    s2 = {**base, ("m.room.topic", ""): "$tb"}

    resolved = sr.resolve([s1, s2], event_map)
    # Bob's later event is unauthorized, so Alice's authorized topic wins.
    assert resolved[("m.room.topic", "")] == "$ta"


def test_unauthorized_power_level_change_is_rejected() -> None:
    event_map, base = _base()
    # Alice raises Bob; Bob tries to raise himself — only Alice's change is valid.
    pl_alice = _ev("$pla", "m.room.power_levels", _ALICE,
                   content={**_PL_CONTENT, "users": {_ALICE: 100, _BOB: 50}},
                   auth=("$c", "$ma", "$pl"), ts=100)
    pl_bob = _ev("$plb", "m.room.power_levels", _BOB,
                 content={**_PL_CONTENT, "users": {_ALICE: 100, _BOB: 100}},
                 auth=("$c", "$mb", "$pl"), ts=200)
    event_map[pl_alice.event_id] = pl_alice
    event_map[pl_bob.event_id] = pl_bob
    s1 = {**base, ("m.room.power_levels", ""): "$pla"}
    s2 = {**base, ("m.room.power_levels", ""): "$plb"}

    resolved = sr.resolve([s1, s2], event_map)
    assert resolved[("m.room.power_levels", "")] == "$pla"
