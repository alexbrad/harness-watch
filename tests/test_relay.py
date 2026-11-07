"""End to end: relay/relay.py under /usr/bin/python3 (as the LaunchDaemon runs it) in front of a
real server, as a name in /etc/hosts reaches it. On a free port here: port 80 needs root."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from test_server_e2e import register
from test_stream_e2e import events, free_port, server  # noqa: F401  (server: fixture)

RELAY = Path(__file__).parent.parent / "relay" / "relay.py"
PYTHON = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
NAME = {"host": "watch.test"}  # the server fixture allows this name, as HARNESS_WATCH_HOSTNAME would


def start_relay(target_port):
    port = free_port()
    proc = subprocess.Popen([PYTHON, "-I", str(RELAY), "127.0.0.1", str(port), str(target_port)])
    deadline = time.time() + 10
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return proc, port
        time.sleep(0.1)
    proc.kill()
    pytest.fail("relay did not start")


@pytest.fixture
def relay(server):  # noqa: F811
    home, target = server
    proc, port = start_relay(target)
    yield home, port
    proc.terminate()
    proc.wait()


def test_page_and_api_through_the_relay(relay):
    home, port = relay
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        register(home, live.pid, name="relayed")
        page = httpx.get(f"http://127.0.0.1:{port}/", headers=NAME, timeout=5)
        assert page.status_code == 200 and "Claude sessions" in page.text
        api = httpx.get(f"http://127.0.0.1:{port}/api/sessions", headers=NAME, timeout=5)
        assert [s["name"] for s in api.json()["sessions"]] == ["relayed"]
    finally:
        live.kill()
        live.wait()


def test_stream_through_the_relay_is_not_buffered(relay):
    home, port = relay
    first = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    second = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        register(home, first.pid, name="first")
        with httpx.stream("GET", f"http://127.0.0.1:{port}/api/stream", headers=NAME, timeout=10) as resp:
            stream = events(resp)
            assert [s["name"] for s in next(stream)["sessions"]] == ["first"]
            register(home, second.pid, name="second")
            t0 = time.time()
            while {s["name"] for s in next(stream)["sessions"]} != {"first", "second"}:
                assert time.time() - t0 < 5
    finally:
        for p in (first, second):
            p.kill()
            p.wait()


def test_dashboard_down_closes_the_connection_instead_of_hanging():
    proc, port = start_relay(free_port())  # nothing listens on the target
    try:
        with pytest.raises(httpx.TransportError):
            httpx.get(f"http://127.0.0.1:{port}/", headers=NAME, timeout=5)
    finally:
        proc.terminate()
        proc.wait()
