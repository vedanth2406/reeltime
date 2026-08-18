"""The --step prompt.

Small, but it is a UI surface: it is what a user drives when they are already
confused, so its keys have to behave exactly as advertised.
"""

import pytest

import reeltime as tape
from reeltime.core import stepper
from reeltime.core.trace import Event
from reeltime.errors import StopReplay


class FakePlayer:
    def __init__(self):
        self._stepping_off = False

    def resolved(self, event, payload):
        return payload


@pytest.fixture
def player():
    return FakePlayer()


@pytest.fixture
def event():
    return Event(i=3, kind="tool", site="agent.py:12",
                 req={"name": "read_file", "args": {"path": "a.md"}})


def test_enter_advances_one_event(monkeypatch, capsys, event, player):
    monkeypatch.setattr("builtins.input", lambda *a: "")
    stepper.interactive(event, player)
    assert "#3" in capsys.readouterr().err
    assert not player._stepping_off


def test_c_stops_prompting_for_the_rest_of_the_run(monkeypatch, event, player):
    monkeypatch.setattr("builtins.input", lambda *a: "c")
    stepper.interactive(event, player)
    assert player._stepping_off

    # Once continued, later events pass straight through.
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("prompted again"))
    stepper.interactive(event, player)


def test_q_stops_the_replay(monkeypatch, event, player):
    monkeypatch.setattr("builtins.input", lambda *a: "q")
    with pytest.raises(StopReplay) as caught:
        stepper.interactive(event, player)
    assert caught.value.stopped_at == 3


def test_s_prints_the_whole_event_then_prompts_again(monkeypatch, capsys, event, player):
    answers = iter(["s", ""])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    stepper.interactive(event, player)
    err = capsys.readouterr().err
    assert '"name": "read_file"' in err


def test_an_unknown_key_shows_the_help(monkeypatch, capsys, event, player):
    answers = iter(["wat", ""])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    stepper.interactive(event, player)
    assert "c continue" in capsys.readouterr().err


def test_a_closed_stdin_continues_rather_than_hanging(monkeypatch, event, player):
    def no_stdin(*args):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_stdin)
    stepper.interactive(event, player)
    assert player._stepping_off


@pytest.mark.parametrize(
    "event,expected",
    [
        (Event(i=0, kind="http", site="a:1",
               req={"method": "GET", "url": "http://x/y"}), "GET http://x/y"),
        (Event(i=0, kind="tool", site="a:1",
               req={"name": "search", "args": {"q": "cats"}}), 'search("q": "cats")'),
        (Event(i=0, kind="rand", site="a:1", req={"name": "random"}), "random"),
    ],
)
def test_one_line_summaries(event, expected):
    assert stepper._one_line(event) == expected
