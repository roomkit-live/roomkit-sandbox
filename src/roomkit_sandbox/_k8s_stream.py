"""Bounded consumption of Kubernetes exec streams.

Closing an exec stream does NOT guarantee termination of its remote process.
This module bounds the API client's work and memory, not the command lifetime.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from roomkit_sandbox._shared import ExecResult

MAX_OUTPUT_BYTES = 1024 * 1024
MAX_TIMEOUT_SECONDS = 300


def consume(
    response: Any,
    payload: str | None,
    deadline: float,
    stopped: threading.Event,
    *,
    expects_status: bool = True,
) -> ExecResult:
    """Own and close the stream on success, timeout, cancellation or failure."""
    stdout: list[str] = []
    stderr: list[str] = []
    size = 0
    try:
        if stopped.is_set() or time.monotonic() >= deadline:
            raise TimeoutError("Sandbox exec client deadline exceeded")
        if payload is not None:
            response.write_stdin(payload)
        while response.is_open():
            remaining = deadline - time.monotonic()
            if stopped.is_set() or remaining <= 0:
                raise TimeoutError("Sandbox exec client deadline exceeded")
            response.update(timeout=min(0.1, remaining))
            for available, read, parts in (
                (response.peek_stdout, response.read_stdout, stdout),
                (response.peek_stderr, response.read_stderr, stderr),
            ):
                if available():
                    chunk = read()
                    size += len(chunk.encode("utf-8"))
                    if size > MAX_OUTPUT_BYTES:
                        # The SDK also retains an aggregate buffer. Ending the
                        # stream here bounds that buffer as well as our result.
                        raise RuntimeError("Sandbox output exceeded the 1 MiB capture limit")
                    parts.append(chunk)
        code = response.returncode if expects_status else 0
        if code is None:
            raise RuntimeError("Sandbox exec disconnected without an exit status")
        return ExecResult(exit_code=code, stdout="".join(stdout), stderr="".join(stderr))
    finally:
        response.close()


def new_api() -> Any:
    """A private SDK client per stream; no shared transport mutation.

    kubernetes v36's in-cluster loader uses the old authorization key while
    generated methods read BearerToken. Refresh both identity and expiry at
    the client's door until upstream ships its loader fix.
    """
    from kubernetes import client, config

    configuration = client.Configuration()
    try:
        config.load_incluster_config(client_configuration=configuration)
    except config.ConfigException:
        config.load_kube_config(client_configuration=configuration)
    else:

        def refresh(cfg: Any) -> None:
            with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as token_file:
                cfg.api_key["BearerToken"] = token_file.read().strip()
            cfg.api_key_prefix["BearerToken"] = "Bearer"

        configuration.refresh_api_key_hook = refresh
        refresh(configuration)
    return client.CoreV1Api(client.ApiClient(configuration=configuration))
