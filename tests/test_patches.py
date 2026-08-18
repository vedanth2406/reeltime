import datetime
import logging
import random
import time
import uuid

import pytest

import reeltime as tape


def kinds_in(run):
    return [e.kind for e in tape.read_trace(run.path).events]


def events_in(run):
    return tape.read_trace(run.path).events


# -- random --------------------------------------------------------------


def test_random_draws_are_recorded(recording):
    value = random.random()
    integer = random.randint(1, 6)
    pick = random.choice(["a", "b"])
    tape.uninstall()

    events = events_in(recording)
    assert [e.req["name"] for e in events] == ["random", "randint", "choice"]
    assert events[0].res["value"] == value
    assert events[1].res["value"] == integer
    assert events[2].res["value"] == pick
    assert events[1].req["args"] == [1, 6]


def test_shuffle_records_the_permutation_not_the_elements(recording):
    items = list(range(8))
    before = list(items)
    random.shuffle(items)
    tape.uninstall()

    event = events_in(recording)[0]
    assert event.req == {"name": "shuffle", "n": 8}
    # Replaying the permutation reproduces the shuffle on any equivalent list.
    assert [before[i] for i in event.res["perm"]] == items


def test_shuffle_permutation_survives_duplicate_elements(recording):
    items = ["a", "a", "b", "b"]
    before = list(items)
    random.shuffle(items)
    tape.uninstall()

    perm = events_in(recording)[0].res["perm"]
    assert sorted(perm) == [0, 1, 2, 3]
    assert [before[i] for i in perm] == items


def test_explicit_random_instances_are_left_alone(recording):
    # Documented limitation: a Random you constructed is one you can seed.
    generator = random.Random(0)
    generator.random()
    tape.uninstall()
    assert kinds_in(recording) == []


# -- uuid ----------------------------------------------------------------


def test_uuid_calls_are_recorded(recording):
    value = uuid.uuid4()
    tape.uninstall()

    event = events_in(recording)[0]
    assert event.kind == "uuid"
    assert event.res["value"] == str(value)


def test_deterministic_uuids_are_not_recorded(recording):
    uuid.uuid5(uuid.NAMESPACE_DNS, "example.com")  # pure function of its args
    tape.uninstall()
    assert kinds_in(recording) == []


# -- time ----------------------------------------------------------------


def test_clock_reads_are_recorded(recording):
    wall = time.time()
    mono = time.monotonic()
    tape.uninstall()

    events = events_in(recording)
    assert [e.req["name"] for e in events] == ["time.time", "time.monotonic"]
    assert events[0].res["value"] == wall
    assert events[1].res["value"] == mono


@pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow")
def test_datetime_now_is_recorded(recording):
    now = datetime.datetime.now()
    utc = datetime.datetime.utcnow()
    tape.uninstall()

    events = events_in(recording)
    assert [e.req["name"] for e in events] == ["datetime.now", "datetime.utcnow"]
    assert events[0].res["value"] == now.isoformat()
    assert events[1].res["value"] == utc.isoformat()


def test_timezone_aware_now_is_recorded_with_its_tz(recording):
    datetime.datetime.now(datetime.timezone.utc)
    tape.uninstall()
    assert events_in(recording)[0].req["tz"] == "UTC"


def test_patched_datetime_still_passes_isinstance(recording):
    now = datetime.datetime.now()
    assert isinstance(now, datetime.datetime)
    # Arithmetic on a datetime returns the *base* C type, so without the
    # metaclass override this is the assertion that would break user code.
    later = now + datetime.timedelta(hours=1)
    assert isinstance(later, datetime.datetime)
    assert isinstance(datetime.datetime(2026, 1, 1), datetime.datetime)
    assert issubclass(type(later), datetime.datetime)


def test_datetime_keeps_working_normally(recording):
    parsed = datetime.datetime.fromisoformat("2026-08-17T12:00:00")
    assert parsed.year == 2026
    assert parsed.strftime("%Y") == "2026"
    assert (datetime.datetime.now() - parsed).total_seconds() != 0


# -- the stdlib filter ---------------------------------------------------


def test_the_runtimes_own_clock_reads_are_ignored(recording):
    # logging timestamps every record; asyncio reads monotonic() on every loop
    # iteration. Recording those would bury the trace.
    logging.getLogger("reeltime-test").warning("hello")
    tape.uninstall()
    assert kinds_in(recording) == []


def test_stdlib_reads_can_be_opted_into(tape_dir):
    run = tape.install(tape_dir=tape_dir, collect_git=False, record_stdlib_ambient=True)
    logging.getLogger("reeltime-test").warning("hello")
    tape.uninstall()
    assert "time" in kinds_in(run)


# -- lifecycle -----------------------------------------------------------


def test_uninstall_restores_every_patched_attribute(tape_dir):
    originals = {
        "random": random.random,
        "shuffle": random.shuffle,
        "uuid4": uuid.uuid4,
        "time": time.time,
        "monotonic": time.monotonic,
        "datetime": datetime.datetime,
    }
    tape.install(tape_dir=tape_dir, collect_git=False)
    assert random.random is not originals["random"]
    assert datetime.datetime is not originals["datetime"]
    tape.uninstall()

    assert random.random is originals["random"]
    assert random.shuffle is originals["shuffle"]
    assert uuid.uuid4 is originals["uuid4"]
    assert time.time is originals["time"]
    assert time.monotonic is originals["monotonic"]
    assert datetime.datetime is originals["datetime"]


def test_patch_groups_can_be_selected(tape_dir):
    run = tape.install(tape_dir=tape_dir, collect_git=False, patch=("uuid",))
    random.random()
    uuid.uuid4()
    time.time()
    tape.uninstall()
    assert kinds_in(run) == ["uuid"]


def test_patching_can_be_disabled_entirely(tape_dir):
    run = tape.install(tape_dir=tape_dir, collect_git=False, patch=())
    random.random()
    assert not hasattr(random.random, "__wrapped__")  # no wrapper installed
    tape.uninstall()
    assert kinds_in(run) == []


def test_nothing_is_recorded_after_uninstall(recording):
    random.random()
    tape.uninstall()
    random.random()
    assert len(events_in(recording)) == 1


# -- numpy ---------------------------------------------------------------

numpy = pytest.importorskip("numpy")


def test_numpy_draws_are_recorded(tape_dir):
    run = tape.install(tape_dir=tape_dir, collect_git=False)
    value = numpy.random.rand(3)
    tape.uninstall()

    event = events_in(run)[0]
    assert event.kind == "rand"
    assert event.req["name"] == "numpy.random.rand"
    assert event.res["value"]["__ndarray__"] == pytest.approx(list(value))
    assert event.res["value"]["shape"] == [3]


def test_numpy_scalars_serialise_as_plain_numbers(tape_dir):
    run = tape.install(tape_dir=tape_dir, collect_git=False)
    numpy.random.normal()
    tape.uninstall()
    assert isinstance(events_in(run)[0].res["value"], float)
