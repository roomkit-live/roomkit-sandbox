"""A stand-in for the smolBSD guest runner, serving on a unix socket.

Speaks the protocol of ``smolbsd/runner.sh`` and runs EXEC scripts with
the host's ``sh``. Run as a script (``python fake_smolbsd_runner.py
<socket>``) it plays a VM's QEMU process: the socket path is on its
command line, as on QEMU's, so the backend's liveness check accepts it.
"""

from __future__ import annotations

import asyncio
import base64
import sys
from collections.abc import Awaitable, Callable

# Answer lines for a request, or None to hang up on the client
Handler = Callable[[list[str]], Awaitable[list[str] | None]]


def b64(data: bytes | str) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return base64.b64encode(raw).decode("ascii")


class FakeRunner:
    """Protocol semantics: PING, ENV (kept in memory), EXEC with timeout."""

    def __init__(self, hang_on_exec: bool = False) -> None:
        self.env_script = ""
        self.hang_on_exec = hang_on_exec

    async def handle(self, parts: list[str]) -> list[str]:
        verb, req_id = parts[0], parts[1]
        if verb == "PING":
            return [f"PONG {req_id}"]
        if verb == "ENV":
            self.env_script = base64.b64decode(parts[2]).decode()
            return [f"OK {req_id}"]
        if verb == "EXEC":
            if self.hang_on_exec:
                await asyncio.sleep(3600)
            rc, out, err = await self._run(int(parts[2]), base64.b64decode(parts[3]).decode())
            return [f"RES {req_id} {rc} {b64(out)} {b64(err)}"]
        return [f"ERR {req_id} unknown request"]

    async def _run(self, timeout: int, script: str) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            self.env_script + script,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, b"", b""
        return proc.returncode or 0, out, err


async def serve(path: str, handler: Handler) -> asyncio.Server:
    """Serve ``handler`` on ``path``: one request line in, its lines out."""

    async def client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while line := await reader.readline():
            answers = await handler(line.decode().rstrip("\n").split(" "))
            if answers is None:
                break
            for answer in answers:
                writer.write(answer.encode() + b"\n")
            await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(client, path, limit=16 * 1024 * 1024)


async def _main(path: str) -> None:
    server = await serve(path, FakeRunner(hang_on_exec="--hang-on-exec" in sys.argv).handle)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1]))
