"""Client side of the roomkit-sandbox runner protocol.

The runner (``smolbsd/runner.sh``, baked into the guest image) reads one
request per line on a VirtIO console port that QEMU exposes on the host
as a unix socket, and answers each with one line. Binary fields are
base64 on a single line::

    PING <id>                          -> PONG <id>
    ENV <id> <b64 sh script>           -> OK <id>
    EXEC <id> <timeout> <b64 script>   -> RES <id> <rc> <b64 stdout> <b64 stderr>
    any request that fails             -> ERR <id> <reason>

Lines that do not answer the pending request are skipped: the guest tty
echoes what arrives before the runner switches it to raw mode, and the
answer to a request abandoned on timeout can still come in later. Ids
are random, so such an answer never matches another client's request.
"""

from __future__ import annotations

import asyncio
import base64
import secrets

from roomkit_sandbox._shared import ExecResult

# The runner cuts each stream at 4 MiB: both in base64 plus framing fit.
_MAX_LINE = 16 * 1024 * 1024


class RunnerError(RuntimeError):
    """The runner refused a request or answered out of protocol."""


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode("ascii")


def _unb64(field: str) -> str:
    return base64.b64decode(field).decode(errors="replace")


class RunnerChannel:
    """Requests to one VM's runner, one at a time, each on its own connection.

    QEMU serves the runner's socket to a single client at a time: a held
    connection would lock out anyone else (a restarted roomkit finding
    the VM again, another worker). A connection per request lets them
    take turns and leaves nothing to recover after a failure.
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._lock = asyncio.Lock()

    async def ping(self, timeout: float) -> None:
        await self._request("PING", [], "PONG", timeout)

    async def set_env(self, script: str, timeout: float) -> None:
        """Store the session environment, sourced before every command."""
        await self._request("ENV", [_b64(script)], "OK", timeout)

    async def execute(self, script: str, timeout: int, grace: float) -> ExecResult:
        """Run ``script`` in the guest.

        The runner enforces ``timeout`` itself (exit code 124); the host
        only gives up ``grace`` seconds later, when the runner is stuck.
        """
        fields = await self._request("EXEC", [str(timeout), _b64(script)], "RES", timeout + grace)
        if len(fields) != 3:
            raise RunnerError(f"malformed EXEC answer ({len(fields)} fields)")
        exit_code, stdout, stderr = fields
        return ExecResult(exit_code=int(exit_code), stdout=_unb64(stdout), stderr=_unb64(stderr))

    async def _request(
        self, verb: str, fields: list[str], answer: str, timeout: float
    ) -> list[str]:
        async with self._lock:
            req_id = secrets.token_hex(6)
            return await asyncio.wait_for(self._exchange(verb, req_id, fields, answer), timeout)

    async def _exchange(self, verb: str, req_id: str, fields: list[str], answer: str) -> list[str]:
        reader, writer = await asyncio.open_unix_connection(self._socket_path, limit=_MAX_LINE)
        try:
            writer.write(" ".join([verb, req_id, *fields]).encode("ascii") + b"\n")
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line.endswith(b"\n"):
                    raise ConnectionError("runner closed the connection")
                parts = line.decode("ascii", errors="replace").rstrip("\r\n").split(" ")
                if len(parts) < 2 or parts[1] != req_id:
                    continue
                if parts[0] == "ERR":
                    raise RunnerError(" ".join(parts[2:]) or f"{verb} failed")
                if parts[0] == answer:
                    return parts[2:]
        finally:
            writer.close()
