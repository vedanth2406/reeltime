import asyncio
import threading

import reeltime as tape
from reeltime.core import spans


def test_default_span_is_root():
    assert spans.current() == "root"


def test_spans_nest():
    with tape.span("plan"):
        assert spans.current() == "root/plan"
        with tape.span("tools"):
            assert spans.current() == "root/plan/tools"
    assert spans.current() == "root"


def test_separators_in_names_are_sanitised():
    with tape.span("a/b"):
        assert spans.current() == "root/a_b"


def test_span_is_restored_after_an_exception():
    try:
        with tape.span("plan"):
            raise ValueError("boom")
    except ValueError:
        pass
    assert spans.current() == "root"


def test_async_tasks_inherit_the_span_they_were_created_in():
    seen = {}

    async def child(name):
        seen[name] = spans.current()

    async def main():
        with tape.span("fanout"):
            await asyncio.gather(child("a"), child("b"))

    asyncio.run(main())
    assert seen == {"a": "root/fanout", "b": "root/fanout"}


def test_a_task_can_open_its_own_span():
    seen = {}

    async def child(name):
        with tape.span(name):
            seen[name] = spans.current()

    async def main():
        with tape.span("fanout"):
            await asyncio.gather(child("a"), child("b"))
        seen["after"] = spans.current()

    asyncio.run(main())
    assert seen == {
        "a": "root/fanout/a",
        "b": "root/fanout/b",
        "after": "root",  # both the task spans and the outer span unwound
    }


def test_threads_do_not_leak_spans_into_each_other():
    seen = {}

    def worker(name):
        with tape.span(name):
            seen[name] = spans.current()

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    with tape.span("main"):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert spans.current() == "root/main"

    # A new thread starts from a fresh context, so it begins at the root.
    assert seen == {"a": "root/a", "b": "root/b"}
