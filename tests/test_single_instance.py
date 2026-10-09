from __future__ import annotations

import re
import threading
import time
import uuid

import pytest
from PySide6.QtCore import QCoreApplication

from portable_manager.single_instance import SingleInstance, default_instance_key


@pytest.fixture(scope="module")
def qapp():
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
def key():
    return f"ppm-test-{uuid.uuid4().hex}"


@pytest.fixture
def instances():
    """Collect instances so each test releases its server at teardown."""
    created: list[SingleInstance] = []
    yield created
    for inst in created:
        inst.release()


def _pump_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_first_acquires_second_is_refused(qapp, key, instances):
    first = SingleInstance(key)
    second = SingleInstance(key)
    instances.extend([first, second])

    assert first.try_acquire() is True
    assert second.try_acquire() is False


def test_second_launch_emits_activation_on_primary(qapp, key, instances):
    first = SingleInstance(key)
    instances.append(first)
    received: list[bool] = []
    first.activation_requested.connect(lambda: received.append(True))
    assert first.try_acquire() is True

    # The second launch must run on its own thread: in a real launch it is a
    # separate process with its own event loop, and a blocking client on this
    # thread would stop the primary's server from accepting the connection.
    results: list[bool] = []
    client = threading.Thread(
        target=lambda: results.append(SingleInstance(key).try_acquire())
    )
    client.start()
    assert _pump_until(lambda: not client.is_alive())
    client.join()

    assert results == [False]
    assert _pump_until(lambda: bool(received))


def test_release_allows_reacquire(qapp, key, instances):
    first = SingleInstance(key)
    assert first.try_acquire() is True
    first.release()

    second = SingleInstance(key)
    instances.append(second)
    assert second.try_acquire() is True


def test_default_instance_key_is_sanitized(monkeypatch):
    monkeypatch.setenv("USERNAME", "Alex Stapleton/../x@y")

    key = default_instance_key()

    assert key == "PortableProgramManager-AlexStapletonxy"
    assert re.fullmatch(r"PortableProgramManager-[A-Za-z0-9_-]*", key)
