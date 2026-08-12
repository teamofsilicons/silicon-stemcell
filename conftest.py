"""Suite-wide guarantees that no test can leave a thread running.

``interface.get_unread_events()`` starts the durable-inbox listener, which is
correct in production and poison in a suite: once the test that started it
returns, its ``InterfaceClient`` patch lifts, the loop reaches the real CLI,
fails, and prints — into whichever unrelated test happens to be running then.
That surfaced as a one-in-ten failure in a test asserting ``print`` was not
called, several files away from the cause.

So every test hands the listener back. Both stops are no-ops when nothing is
running, which is the case for all but a handful of tests.
"""
import threading

import pytest

from interface import inbox


@pytest.fixture(autouse=True)
def _no_leaked_interface_threads():
    yield
    inbox.stop_listener()
    inbox.stop_runtime_file_watch()


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """Name any thread that outlived the suite, rather than letting it hide."""
    leaked = [
        thread.name
        for thread in threading.enumerate()
        if thread is not threading.main_thread()
        and not thread.name.startswith("best-effort-outbox")
    ]
    if leaked:
        print(f"\n[conftest] threads still alive at session end: {', '.join(leaked)}")
