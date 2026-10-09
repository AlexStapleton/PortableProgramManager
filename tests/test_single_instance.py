from __future__ import annotations

import re
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

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

    # A second launch is a separate process in real life, so test exactly that
    # (an in-process client thread races the primary's event loop).
    script = (
        "import sys\n"
        "from PySide6.QtCore import QCoreApplication\n"
        "from portable_manager.single_instance import SingleInstance\n"
        "app = QCoreApplication([])\n"
        f"print(SingleInstance({key!r}).try_acquire())\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    client = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, env=env
    )
    assert _pump_until(lambda: client.poll() is not None, timeout=20)
    assert client.stdout.read().strip() == "False"
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
