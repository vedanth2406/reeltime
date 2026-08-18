import datetime
import decimal
import uuid
from dataclasses import dataclass
from pathlib import Path

from reeltime.core.serial import to_jsonable


def test_scalars_pass_through():
    assert to_jsonable({"a": 1, "b": "x", "c": True, "d": None, "e": 1.5}) == {
        "a": 1, "b": "x", "c": True, "d": None, "e": 1.5,
    }


def test_non_finite_floats_are_tagged_not_emitted():
    # json.dumps would happily write bare NaN, which is not valid JSON and
    # would break every other tool that reads the trace.
    assert to_jsonable(float("nan")) == {"__float__": "nan"}
    assert to_jsonable(float("inf")) == {"__float__": "inf"}
    assert to_jsonable(float("-inf")) == {"__float__": "-inf"}


def test_bytes_are_base64_tagged():
    out = to_jsonable(b"hello")
    assert out == {"__bytes__": "aGVsbG8=", "len": 5}


def test_containers_are_normalised():
    assert to_jsonable((1, 2)) == [1, 2]
    assert to_jsonable({1: "a"}) == {"1": "a"}
    assert to_jsonable({"b", "a"}) == {"__set__": ["a", "b"]}


def test_common_stdlib_types():
    assert to_jsonable(Path("/tmp/x")) == "/tmp/x"
    assert to_jsonable(decimal.Decimal("1.5")) == {"__decimal__": "1.5"}
    assert to_jsonable(datetime.timedelta(seconds=90)) == {"__timedelta__": 90.0}
    value = uuid.UUID(int=7)
    assert to_jsonable(value) == {"__uuid__": str(value)}
    stamp = datetime.datetime(2026, 8, 17, 12, 0)
    assert to_jsonable(stamp) == {"__datetime__": "2026-08-17T12:00:00"}


def test_dataclasses_and_model_objects():
    @dataclass
    class Reply:
        content: str
        tokens: int

    assert to_jsonable(Reply("hi", 3)) == {"content": "hi", "tokens": 3}

    class PydanticLike:
        def model_dump(self):
            return {"role": "assistant"}

    assert to_jsonable(PydanticLike()) == {"role": "assistant"}


def test_unknown_objects_degrade_to_a_tagged_repr():
    class Weird:
        def __repr__(self):
            return "<weird>"

    out = to_jsonable(Weird())
    assert out["__repr__"] == "<weird>"
    assert out["__type__"].endswith("Weird")


def test_cycles_do_not_recurse_forever():
    node = {"name": "a"}
    node["self"] = node
    assert to_jsonable(node) == {"name": "a", "self": {"__cycle__": "dict"}}


def test_serialisation_never_raises():
    class Hostile:
        def __repr__(self):
            raise RuntimeError("no")

        def model_dump(self):
            raise RuntimeError("no")

    assert "__repr__" in to_jsonable(Hostile())
