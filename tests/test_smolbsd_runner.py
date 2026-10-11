"""Tests for the runner protocol client, against an in-process fake runner."""

from __future__ import annotations

import asyncio

import pytest

from roomkit_sandbox._smolbsd_runner import RunnerChannel, RunnerError
from tests.fake_smolbsd_runner import FakeRunner, b64, serve


@pytest.fixture
def sock(tmp_path):
    return str(tmp_path / "runner.sock")


async def test_ping_env_and_exec(sock):
    runner = FakeRunner()
    async with await serve(sock, runner.handle):
        channel = RunnerChannel(sock)
        await channel.ping(1)
        await channel.set_env("export TOKEN='a b'\n", 1)
        result = await channel.execute('echo "out $TOKEN"; echo err >&2; exit 3', 5, 1)
    assert (result.exit_code, result.stdout, result.stderr) == (3, "out a b\n", "err\n")


async def test_skips_echoed_request_and_stale_answers(sock):
    async def handler(parts):
        req_id = parts[1]
        return [
            " ".join(parts),  # tty echo before the runner went raw
            f"RES 0123456789ab 0 {b64('stale')} ",  # answer to another request
            f"RES {req_id} 0 {b64('fresh')} ",
        ]

    async with await serve(sock, handler):
        channel = RunnerChannel(sock)
        result = await channel.execute("true", 5, 1)
    assert result.stdout == "fresh"
    assert result.stderr == ""


async def test_err_answer_raises(sock):
    async with await serve(sock, lambda parts: _async([f"ERR {parts[1]} cannot store"])):
        channel = RunnerChannel(sock)
        with pytest.raises(RunnerError, match="cannot store"):
            await channel.set_env("export A=1\n", 1)


async def test_malformed_exec_answer_raises(sock):
    async with await serve(sock, lambda parts: _async([f"RES {parts[1]} 0"])):
        channel = RunnerChannel(sock)
        with pytest.raises(RunnerError, match="malformed"):
            await channel.execute("true", 5, 1)


async def test_next_request_works_after_a_timeout(sock):
    calls = []

    async def handler(parts):
        calls.append(parts[0])
        if len(calls) == 1:
            await asyncio.sleep(1)  # first answer comes too late
        return [f"PONG {parts[1]}"]

    async with await serve(sock, handler):
        channel = RunnerChannel(sock)
        with pytest.raises(TimeoutError):
            await channel.ping(0.2)
        await channel.ping(2)
    assert calls == ["PING", "PING"]


async def test_closed_connection_raises_then_next_request_works(sock):
    calls = []

    async def handler(parts):
        calls.append(parts[0])
        if len(calls) == 2:
            return None  # hang up instead of answering
        return [f"PONG {parts[1]}"]

    async with await serve(sock, handler):
        channel = RunnerChannel(sock)
        await channel.ping(1)
        with pytest.raises(ConnectionError):
            await channel.ping(1)
        await channel.ping(1)
    assert calls == ["PING", "PING", "PING"]


async def _async(lines):
    return lines
