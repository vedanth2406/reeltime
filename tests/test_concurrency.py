import asyncio
import random
import threading

import reeltime as tape


def test_threads_record_into_one_trace_without_colliding(recording):
    threads_count, per_thread = 8, 25

    def worker(name):
        for n in range(per_thread):
            tape.record_event("tool", {"name": name, "n": n})

    threads = [
        threading.Thread(target=worker, args=("w{}".format(i),))
        for i in range(threads_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    tape.uninstall()

    events = tape.read_trace(recording.path).events
    total = threads_count * per_thread
    assert len(events) == total
    # Indices are unique and gapless: no event was dropped or double-numbered.
    assert [e.i for e in events] == list(range(total))
    for i in range(threads_count):
        assert len([e for e in events if e.name == "w{}".format(i)]) == per_thread


def test_a_worker_thread_is_still_recorded(recording):
    # Threads start with a fresh context, so a ContextVar-only tape would go
    # silently blind here while the patches stayed installed.
    def worker():
        random.random()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    tape.uninstall()

    assert [e.kind for e in tape.read_trace(recording.path).events] == ["rand"]


def test_concurrent_tasks_are_separated_by_span(recording):
    async def tool(name, delay):
        with tape.span(name):
            await asyncio.sleep(delay)
            tape.record_event("tool", {"name": name}, {"value": name.upper()})

    async def main():
        # Deliberately finishing out of creation order.
        await asyncio.gather(tool("a", 0.03), tool("b", 0.01), tool("c", 0.02))

    asyncio.run(main())
    tape.uninstall()

    events = tape.read_trace(recording.path).events
    assert {e.span for e in events} == {"root/a", "root/b", "root/c"}
    # Each span holds exactly one event, so replay may reorder them freely.
    assert [e.name for e in events] == ["b", "c", "a"]


def test_events_in_one_span_keep_their_recorded_order(recording):
    async def step(n):
        tape.record_event("tool", {"name": "step", "n": n})

    async def main():
        with tape.span("sequential"):
            for n in range(5):
                await step(n)

    asyncio.run(main())
    tape.uninstall()

    events = tape.read_trace(recording.path).events
    assert [e.req["n"] for e in events] == [0, 1, 2, 3, 4]
    assert {e.span for e in events} == {"root/sequential"}


def test_every_line_of_a_concurrent_trace_is_valid_json(recording):
    def worker():
        for _ in range(40):
            tape.record_event("tool", {"name": "t", "payload": "x" * 200})

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    tape.uninstall()

    result = tape.read_trace(recording.path)
    assert not result.truncated  # interleaved writes never tore a line
    assert len(result) == 240
