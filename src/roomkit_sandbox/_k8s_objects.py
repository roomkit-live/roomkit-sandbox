"""Kubernetes objects for persistent workspaces and disposable command pods.

This is the Kubernetes backend's object construction, not another executor.
"""

from __future__ import annotations

import hashlib
import shlex
from typing import Any

OWNER = "roomkit-sandbox"
FORMAT = "roomkit.dev/workspace-version"
SESSION = "roomkit.dev/session"
WORKSPACE_UID = "roomkit.dev/workspace-uid"
FORMAT_VERSION = "1"
STARTUP_SECONDS = 60
RESULT_RETENTION_SECONDS = 120
# Each pod has its own tmpfs; this is not a host temporary-file path.
_TMP_PATH = "/tmp"  # nosec B108


def workspace_name(session_id: str) -> str:
    return "workspace-" + hashlib.sha256(session_id.encode()).hexdigest()[:40]


def command_name(workspace: str) -> str:
    return "command-" + workspace.removeprefix("workspace-")


def metadata(session_id: str, labels: dict[str, str]) -> dict[str, Any]:
    return {
        "name": workspace_name(session_id),
        "labels": {**labels, "managed-by": OWNER},
        "annotations": {FORMAT: FORMAT_VERSION, SESSION: session_id},
    }


def claim(session_id: str, labels: dict[str, str], storage_class: str | None) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "4Gi"}},
    }
    if storage_class is not None:
        spec["storageClassName"] = storage_class
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": metadata(session_id, labels),
        "spec": spec,
    }


def environment(workspace: Any, env: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "immutable": True,
        "metadata": {
            "name": workspace.metadata.name,
            "labels": workspace.metadata.labels,
            "ownerReferences": [
                {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "name": workspace.metadata.name,
                    "uid": workspace.metadata.uid,
                }
            ],
        },
        "stringData": env,
    }


def command_pod(
    workspace: Any,
    *,
    image: str,
    service_account: str,
    image_pull_secret: str,
    runtime_class: str | None,
    command: list[str],
    workdir: str,
    nonce: str,
    timeout: int,
) -> dict[str, Any]:
    """PID 1 waits for the matching attachment before executing untrusted work.

    The nonce binds an attachment to this invocation even if its pod name was
    deleted and recreated concurrently. Credentials are the second stdin line,
    never a pod argument, annotation, log entry or persistent Secret.
    """
    wrapper = (
        f'IFS= read -r __rks_nonce && test "$__rks_nonce" = {shlex.quote(nonce)} && '
        'IFS= read -r __rks_env && eval "$(printf %s "$__rks_env" | base64 -d)" && '
        "unset __rks_nonce __rks_env && "
        f"cd {shlex.quote(workdir)} && exec {shlex.join(command)} </dev/null"
    )
    security = {
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    resources = {
        "requests": {"cpu": "500m", "memory": "256Mi", "ephemeral-storage": "256Mi"},
        "limits": {"cpu": "1", "memory": "512Mi", "ephemeral-storage": "1Gi"},
    }
    pod: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": command_name(workspace.metadata.name),
            "labels": {
                **(workspace.metadata.labels or {}),
                "app": OWNER,
                WORKSPACE_UID: workspace.metadata.uid,
            },
            "ownerReferences": [
                {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "name": workspace.metadata.name,
                    "uid": workspace.metadata.uid,
                }
            ],
        },
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "serviceAccountName": service_account,
            "terminationGracePeriodSeconds": 1,
            # A creator that dies before attach still leaves a bounded pod.
            "activeDeadlineSeconds": timeout + STARTUP_SECONDS,
            "securityContext": {"fsGroup": 1000, "fsGroupChangePolicy": "OnRootMismatch"},
            "volumes": [
                {"name": "state", "persistentVolumeClaim": {"claimName": workspace.metadata.name}},
                {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "256Mi"}},
            ],
            "initContainers": [
                {
                    "name": "workspace",
                    "image": image,
                    "command": [
                        "sh",
                        "-ec",
                        "mkdir -p /state/workspace /state/home /state/venv; "
                        "if [ ! -f /state/venv/.initialized ]; then "
                        "cp -a /opt/venv/. /state/venv/; touch /state/venv/.initialized; fi",
                    ],
                    "securityContext": security,
                    "resources": resources,
                    "volumeMounts": [
                        {"name": "state", "mountPath": "/state"},
                        {"name": "tmp", "mountPath": _TMP_PATH},
                    ],
                }
            ],
            "containers": [
                {
                    "name": "sandbox",
                    "image": image,
                    "command": ["sh", "-c", wrapper],
                    "stdin": True,
                    "stdinOnce": True,
                    "tty": False,
                    "envFrom": [{"secretRef": {"name": workspace.metadata.name}}],
                    "workingDir": "/workspace",
                    "securityContext": security,
                    "resources": resources,
                    "volumeMounts": [
                        {"name": "state", "mountPath": "/workspace", "subPath": "workspace"},
                        {"name": "state", "mountPath": "/home/sandbox", "subPath": "home"},
                        {"name": "state", "mountPath": "/opt/venv", "subPath": "venv"},
                        {"name": "tmp", "mountPath": _TMP_PATH},
                    ],
                }
            ],
        },
    }
    if runtime_class is not None:
        pod["spec"]["runtimeClassName"] = runtime_class
    if image_pull_secret:
        pod["spec"]["imagePullSecrets"] = [{"name": image_pull_secret}]
    return pod


def command_job(workspace: Any, **kwargs: Any) -> dict[str, Any]:
    """The existing Job controller owns unscheduled deadlines and orphan GC.

    A Job holds the workspace's command name until its caller acknowledges the
    result, or its TTL expires. Its generated pod name is never reused.
    """
    pod = command_pod(workspace, **kwargs)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": pod["metadata"],
        "spec": {
            "backoffLimit": 0,
            "parallelism": 1,
            "completions": 1,
            "activeDeadlineSeconds": pod["spec"]["activeDeadlineSeconds"],
            "ttlSecondsAfterFinished": RESULT_RETENTION_SECONDS,
            "template": {
                # Only the Job owns its generated pod. A second PVC owner
                # would keep that pod alive after the Job was deleted.
                "metadata": {"labels": pod["metadata"]["labels"]},
                "spec": pod["spec"],
            },
        },
    }
