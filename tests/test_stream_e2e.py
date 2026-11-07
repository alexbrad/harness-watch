"""End to end over a real socket: uvicorn in a subprocess, the page's SSE stream, real `ps`."""

import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from test_server_e2e import register


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    (tmp_path / "sessions").mkdir()
    port = free_port()
    code = (
        "import uvicorn; from harness_watch import server; "
        f"uvicorn.run(server.create_app(port={port}, hostname='watch.test'), host='127.0.0.1', port={port}, log_level='warning')"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], env={**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path)})
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{port}/api/sessions", timeout=1)
            break
        except httpx.TransportError:
            time.sleep(0.1)
    yield tmp_path, port
    proc.terminate()
    proc.wait()


def events(resp):
    for line in resp.iter_lines():
        if line.startswith("data: "):
            yield json.loads(line[6:])


def test_new_session_reaches_open_stream_within_seconds(server):
    home, port = server
    first = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    second = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        register(home, first.pid, name="first")
        with httpx.stream("GET", f"http://127.0.0.1:{port}/api/stream", timeout=10) as resp:
            stream = events(resp)
            assert [s["name"] for s in next(stream)["sessions"]] == ["first"]

            register(home, second.pid, name="second")
            t0 = time.time()
            names = []
            while "second" not in names:
                names = [s["name"] for s in next(stream)["sessions"]]
            assert time.time() - t0 < 3
    finally:
        first.kill()
        second.kill()


def test_key_flow_over_a_real_socket(tmp_path):
    """The real server: no key, no stream; the local file's POST gives the browser the key; the
    stream then flows with it."""
    (tmp_path / "sessions").mkdir()
    port = free_port()
    code = ("import uvicorn; from harness_watch import server; "
            f"uvicorn.run(server.create_app(port={port}, page_key='k-real'), host='127.0.0.1', port={port}, log_level='warning')")
    proc = subprocess.Popen([sys.executable, "-c", code], env={**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path)})
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 10
        while True:
            try:
                assert httpx.get(f"{base}/api/stream", timeout=1).status_code == 401
                break
            except httpx.TransportError:
                assert time.time() < deadline
                time.sleep(0.1)
        kept = httpx.post(f"{base}/__key", data={"key": "k-real"}, headers={"origin": "null"}, timeout=5)
        assert kept.status_code == 200 and "k-real" in kept.text
        with httpx.stream("GET", f"{base}/api/stream?key=k-real", timeout=10) as resp:
            assert resp.status_code == 200 and "sessions" in next(events(resp))
    finally:
        proc.terminate()
        proc.wait()
