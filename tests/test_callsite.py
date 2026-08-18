import logging
import os

from reeltime.core import callsite


def _helper():
    return callsite.caller(1)


class Planner:
    def step(self):
        return callsite.caller(1)


def test_site_names_the_calling_file_and_line():
    site = _helper()
    assert site.file.endswith("test_callsite.py")
    assert site.lineno == 8  # the `return callsite.caller(1)` line in _helper
    assert site.site == "{}:{}".format(site.file, site.lineno)


def test_qualname_includes_the_enclosing_function():
    assert _helper().qualname == "_helper"


def test_qualname_includes_the_class_for_methods():
    # co_qualname on 3.11+, reconstructed from `self` before that.
    assert Planner().step().qualname.endswith("step")
    assert "Planner" in Planner().step().qualname


def test_display_paths_are_relative_to_the_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    callsite.clear_cache()
    assert callsite._display(str(tmp_path / "agent.py")) == "agent.py"
    # Outside the project, an absolute path is the only honest answer.
    assert os.path.isabs(callsite._display("/elsewhere/lib.py"))


def test_library_frames_are_identified():
    # The ambient patches lean on this to ignore clock reads made by the
    # runtime and by installed packages, which are not the agent's doing.
    import httpx

    assert callsite._is_stdlib(logging.__file__)
    assert callsite._is_library(logging.__file__)
    assert callsite._is_library(httpx.__file__)  # site-packages, not stdlib
    assert not callsite._is_library(__file__)
    assert not _helper().is_library


def test_frames_inside_reeltime_are_never_reported():
    # caller() runs inside the package and must walk out of it before
    # answering, or every event would blame reeltime for itself.
    site = _helper()
    assert site.file.endswith("test_callsite.py")
    assert "reeltime" not in site.file
