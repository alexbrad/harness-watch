"""Relays port 80 of 127.0.0.2 to harness-watch on 127.0.0.1:8787, so a name in /etc/hosts opens it
without a port.

Run by the root LaunchDaemon harness-watch.relay: adds 127.0.0.2 to loopback, binds port 80 (only
root may), then runs as nobody. A pf forward would do the same, but VPN and firewall apps that load
their own pf rules drop it. Uses only the standard library of
/usr/bin/python3 (3.9). Arguments, for tests: listen host, listen port, target port.
"""

import asyncio
import os
import pwd
import socket
import subprocess
import sys
import time

TARGET_HOST = "127.0.0.1"
CHUNK = 65536


def listen(host: str, port: int) -> socket.socket:
    if os.geteuid() == 0:
        subprocess.run(["/sbin/ifconfig", "lo0", "alias", host, "up"], check=False)
    for attempt in range(30):  # at boot the alias or the network stack may not be ready yet
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.listen(128)
            return sock
        except OSError as e:
            sock.close()
            if attempt == 29:
                sys.exit(f"cannot listen on {host}:{port}: {e}")
            time.sleep(2)


def drop_root() -> None:
    if os.geteuid() == 0:
        nobody = pwd.getpwnam("nobody")
        os.setgroups([])
        os.setgid(nobody.pw_gid)
        os.setuid(nobody.pw_uid)


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


def relay(target_port: int):
    async def handle(client_r, client_w):
        try:
            up_r, up_w = await asyncio.open_connection(TARGET_HOST, target_port)
        except OSError:  # dashboard down: the browser shows "connection reset", not a hang
            client_w.close()
            return
        await asyncio.gather(pipe(client_r, up_w), pipe(up_r, client_w))

    return handle


async def serve(sock: socket.socket, target_port: int) -> None:
    server = await asyncio.start_server(relay(target_port), sock=sock)
    print(f"relaying {sock.getsockname()[0]}:{sock.getsockname()[1]} to {TARGET_HOST}:{target_port}", flush=True)
    async with server:
        await server.serve_forever()


def main() -> None:
    host, port, target = sys.argv[1:] or ["127.0.0.2", "80", "8787"]
    sock = listen(host, int(port))
    drop_root()
    asyncio.run(serve(sock, int(target)))


if __name__ == "__main__":
    main()
