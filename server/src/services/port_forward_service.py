# Copyright 2025 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import logging
import socket
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Set

from fastapi import status
from fastapi.exceptions import HTTPException
from kubernetes import stream
from kubernetes.client import CoreV1Api

from src.api.schema import PortForwardInfo, PortForwardListResponse
from src.services.constants import SANDBOX_ID_LABEL, SandboxErrorCodes

logger = logging.getLogger(__name__)


@dataclass
class PortForwardState:
    server: asyncio.Server
    sandbox_id: str
    local_port: int
    remote_port: int
    namespace: str
    pod_name: str
    created_at: datetime
    active_connections: Set = field(default_factory=set)


class PortForwardService:
    def __init__(self, k8s_client: Any, namespace: str, workload_provider: Any):
        self._k8s_client = k8s_client
        self._core_v1_api: CoreV1Api = k8s_client.get_core_v1_api()
        self._namespace = namespace
        self._workload_provider = workload_provider
        self._registry: Dict[str, Dict[int, PortForwardState]] = {}

    async def create_port_forward(
        self, sandbox_id: str, local_port: int, remote_port: int
    ) -> PortForwardInfo:
        if self._find_state_by_local_port(local_port) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": SandboxErrorCodes.PORT_FORWARD_PORT_CONFLICT,
                    "message": f"Local port {local_port} is already in use.",
                },
            )

        try:
            pod_name = self._resolve_pod_name(sandbox_id)
            server = await asyncio.start_server(
                lambda reader, writer: self._handle_connection(
                    reader,
                    writer,
                    sandbox_id,
                    local_port,
                ),
                host="127.0.0.1",
                port=local_port,
            )
        except HTTPException:
            raise
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": SandboxErrorCodes.PORT_FORWARD_PORT_CONFLICT,
                    "message": f"Failed to bind local port {local_port}: {exc}",
                },
            ) from exc
        except Exception as exc:
            logger.exception("Failed to create port-forward for sandbox %s", sandbox_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "code": SandboxErrorCodes.PORT_FORWARD_K8S_ERROR,
                    "message": f"Failed to create port-forward: {exc}",
                },
            ) from exc

        state = PortForwardState(
            server=server,
            sandbox_id=sandbox_id,
            local_port=local_port,
            remote_port=remote_port,
            namespace=self._namespace,
            pod_name=pod_name,
            created_at=datetime.now(timezone.utc),
        )
        self._registry.setdefault(sandbox_id, {})[local_port] = state
        logger.info(
            "Created port-forward sandbox=%s local_port=%s remote_port=%s pod=%s",
            sandbox_id,
            local_port,
            remote_port,
            pod_name,
        )
        return self._build_info(state)

    def list_port_forwards(self, sandbox_id: str) -> PortForwardListResponse:
        states = self._registry.get(sandbox_id, {})
        return PortForwardListResponse(
            portForwards=[
                self._build_info(state)
                for _, state in sorted(states.items(), key=lambda item: item[0])
            ]
        )

    async def delete_port_forward(self, sandbox_id: str, local_port: int) -> None:
        state = self._registry.get(sandbox_id, {}).get(local_port)
        if state is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": SandboxErrorCodes.PORT_FORWARD_NOT_FOUND,
                    "message": (
                        f"Port-forward for sandbox '{sandbox_id}' on local port {local_port} not found"
                    ),
                },
            )

        try:
            state.server.close()
            await state.server.wait_closed()
            # Close all active tunneled connections
            for writer in list(state.active_connections):
                with suppress(Exception):
                    writer.close()
                    await writer.wait_closed()
        finally:
            sandbox_registry = self._registry.get(sandbox_id)
            if sandbox_registry is not None:
                sandbox_registry.pop(local_port, None)
                if not sandbox_registry:
                    self._registry.pop(sandbox_id, None)

        logger.info(
            "Deleted port-forward sandbox=%s local_port=%s remote_port=%s",
            sandbox_id,
            local_port,
            state.remote_port,
        )

    async def cleanup_sandbox(self, sandbox_id: str) -> None:
        local_ports = list(self._registry.get(sandbox_id, {}).keys())
        for local_port in local_ports:
            with suppress(HTTPException):
                await self.delete_port_forward(sandbox_id, local_port)

    async def cleanup_all(self) -> None:
        for sandbox_id in list(self._registry.keys()):
            await self.cleanup_sandbox(sandbox_id)

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        sandbox_id: str,
        local_port: int,
    ) -> None:
        state = self._registry.get(sandbox_id, {}).get(local_port)
        if state is None:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            return

        connection_id = writer
        state.active_connections.add(connection_id)
        portforward_client = None
        portforward_socket = None

        try:
            portforward_client, portforward_socket = await asyncio.to_thread(
                self._open_portforward_socket,
                state.pod_name,
                state.remote_port,
            )

            tasks = {
                asyncio.create_task(self._copy_client_to_pod(reader, portforward_socket)),
                asyncio.create_task(self._copy_pod_to_client(portforward_socket, writer)),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        except Exception:
            logger.exception(
                "Port-forward connection failed sandbox=%s local_port=%s",
                sandbox_id,
                local_port,
            )
        finally:
            state.active_connections.discard(connection_id)
            await asyncio.to_thread(
                self._close_portforward_resources,
                portforward_socket,
                portforward_client,
            )
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    def _resolve_pod_name(self, sandbox_id: str) -> str:
        workload = self._workload_provider.get_workload(
            sandbox_id=sandbox_id,
            namespace=self._namespace,
        )
        if not workload:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": SandboxErrorCodes.PORT_FORWARD_SANDBOX_NOT_FOUND,
                    "message": f"Sandbox '{sandbox_id}' not found",
                },
            )

        pods = self._k8s_client.list_pods(
            namespace=self._namespace,
            label_selector=f"{SANDBOX_ID_LABEL}={sandbox_id}",
        )
        if pods:
            pod_name = self._extract_metadata_name(pods[0])
            if pod_name:
                return pod_name

        workload_name = self._extract_metadata_name(workload)
        if workload_name:
            return workload_name

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": SandboxErrorCodes.PORT_FORWARD_SANDBOX_NOT_FOUND,
                "message": f"Sandbox '{sandbox_id}' Pod not found",
            },
        )

    def _find_state_by_local_port(self, local_port: int) -> PortForwardState | None:
        for states in self._registry.values():
            state = states.get(local_port)
            if state is not None:
                return state
        return None

    def _build_info(self, state: PortForwardState) -> PortForwardInfo:
        return PortForwardInfo(
            sandboxId=state.sandbox_id,
            localPort=state.local_port,
            remotePort=state.remote_port,
            createdAt=state.created_at,
        )

    def _open_portforward_socket(self, pod_name: str, remote_port: int) -> tuple[Any, Any]:
        portforward_client = stream.portforward(
            self._core_v1_api,
            pod_name,
            self._namespace,
            ports=[remote_port],
        )
        portforward_socket = portforward_client.socket(remote_port)
        if hasattr(portforward_socket, "settimeout"):
            with suppress(Exception):
                portforward_socket.settimeout(0.25)
        return portforward_client, portforward_socket

    async def _copy_client_to_pod(
        self, reader: asyncio.StreamReader, portforward_socket: Any
    ) -> None:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                return
            await asyncio.to_thread(portforward_socket.sendall, chunk)

    async def _copy_pod_to_client(
        self, portforward_socket: Any, writer: asyncio.StreamWriter
    ) -> None:
        while not writer.is_closing():
            chunk = await asyncio.to_thread(self._recv_from_socket, portforward_socket)
            if chunk is None:
                continue
            if not chunk:
                return
            writer.write(chunk)
            await writer.drain()

    def _recv_from_socket(self, portforward_socket: Any) -> bytes | None:
        try:
            return portforward_socket.recv(65536)
        except (TimeoutError, socket.timeout, BlockingIOError):
            return None

    def _close_portforward_resources(
        self, portforward_socket: Any, portforward_client: Any
    ) -> None:
        if portforward_socket is not None and hasattr(portforward_socket, "close"):
            with suppress(Exception):
                portforward_socket.close()
        if portforward_client is not None and hasattr(portforward_client, "close"):
            with suppress(Exception):
                portforward_client.close()

    def _extract_metadata_name(self, resource: Any) -> str | None:
        metadata = None
        if isinstance(resource, dict):
            metadata = resource.get("metadata")
            if isinstance(metadata, dict):
                name = metadata.get("name")
                return name if isinstance(name, str) and name else None
            return None

        metadata = getattr(resource, "metadata", None)
        name = getattr(metadata, "name", None)
        return name if isinstance(name, str) and name else None
