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

"""Tests for port-forward schema models and PortForwardService."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import status
from fastapi.exceptions import HTTPException
import pytest
from pydantic import ValidationError

from src.api.schema import CreatePortForwardRequest, PortForwardInfo, PortForwardListResponse
from src.services.constants import SandboxErrorCodes
from src.services.port_forward_service import PortForwardService, PortForwardState


class TestPortForwardSchemas:
    def test_create_request_valid(self):
        req = CreatePortForwardRequest(localPort=8080, remotePort=44772)
        assert req.local_port == 8080
        assert req.remote_port == 44772

    def test_create_request_via_alias(self):
        req = CreatePortForwardRequest(**{"localPort": 8080, "remotePort": 44772})
        assert req.local_port == 8080

    def test_invalid_port_zero(self):
        with pytest.raises(ValidationError):
            CreatePortForwardRequest(localPort=0, remotePort=44772)

    def test_invalid_port_too_large(self):
        with pytest.raises(ValidationError):
            CreatePortForwardRequest(localPort=70000, remotePort=44772)

    def test_invalid_port_negative(self):
        with pytest.raises(ValidationError):
            CreatePortForwardRequest(localPort=-1, remotePort=44772)

    def test_invalid_remote_port_zero(self):
        with pytest.raises(ValidationError):
            CreatePortForwardRequest(localPort=8080, remotePort=0)

    def test_port_boundary_min(self):
        req = CreatePortForwardRequest(localPort=1, remotePort=1)
        assert req.local_port == 1

    def test_port_boundary_max(self):
        req = CreatePortForwardRequest(localPort=65535, remotePort=65535)
        assert req.local_port == 65535


class TestErrorCodes:
    def test_port_conflict(self):
        assert SandboxErrorCodes.PORT_FORWARD_PORT_CONFLICT == "PORT_FORWARD::PORT_CONFLICT"

    def test_not_supported(self):
        assert SandboxErrorCodes.PORT_FORWARD_NOT_SUPPORTED == "PORT_FORWARD::NOT_SUPPORTED"

    def test_not_found(self):
        assert SandboxErrorCodes.PORT_FORWARD_NOT_FOUND == "PORT_FORWARD::NOT_FOUND"

    def test_sandbox_not_found(self):
        assert SandboxErrorCodes.PORT_FORWARD_SANDBOX_NOT_FOUND == "PORT_FORWARD::SANDBOX_NOT_FOUND"

    def test_k8s_error(self):
        assert SandboxErrorCodes.PORT_FORWARD_K8S_ERROR == "PORT_FORWARD::K8S_ERROR"

    def test_invalid_port(self):
        assert SandboxErrorCodes.PORT_FORWARD_INVALID_PORT == "PORT_FORWARD::INVALID_PORT"


@pytest.fixture
def mock_k8s_client():
    client = MagicMock()
    client.get_core_v1_api.return_value = MagicMock()
    client.list_pods.return_value = [
        SimpleNamespace(metadata=SimpleNamespace(name="sandbox-test-123-pod"))
    ]
    return client


@pytest.fixture
def mock_workload_provider():
    provider = MagicMock()
    provider.get_workload.return_value = {"metadata": {"name": "sandbox-test-123"}}
    return provider


def make_mock_server() -> MagicMock:
    server = MagicMock()
    server.close = MagicMock()
    server.wait_closed = AsyncMock()
    server.sockets = [MagicMock()]
    return server


class TestPortForwardService:
    def test_can_instantiate_with_mocked_dependencies(self, mock_k8s_client, mock_workload_provider):
        service = PortForwardService(mock_k8s_client, "default", mock_workload_provider)

        assert service._k8s_client is mock_k8s_client
        assert service._workload_provider is mock_workload_provider
        assert service._namespace == "default"
        assert service._registry == {}

    @pytest.mark.asyncio
    async def test_create_and_list_port_forward(self, mock_k8s_client, mock_workload_provider):
        service = PortForwardService(mock_k8s_client, "default", mock_workload_provider)
        mock_server = make_mock_server()

        with patch("src.services.port_forward_service.asyncio.start_server", new=AsyncMock(return_value=mock_server)) as mock_start_server:
            info = await service.create_port_forward("sandbox-test-123", 9090, 44772)

        assert info == PortForwardInfo(
            sandboxId="sandbox-test-123",
            localPort=9090,
            remotePort=44772,
            createdAt=info.created_at,
        )
        assert info.created_at.tzinfo == timezone.utc
        assert mock_workload_provider.get_workload.call_args.kwargs == {
            "sandbox_id": "sandbox-test-123",
            "namespace": "default",
        }
        assert mock_k8s_client.list_pods.call_args.kwargs == {
            "namespace": "default",
            "label_selector": "opensandbox.io/id=sandbox-test-123",
        }
        assert mock_start_server.await_args is not None
        assert mock_start_server.await_args.kwargs["port"] == 9090

        response = service.list_port_forwards("sandbox-test-123")
        assert response == PortForwardListResponse(
            portForwards=[
                PortForwardInfo(
                    sandboxId="sandbox-test-123",
                    localPort=9090,
                    remotePort=44772,
                    createdAt=info.created_at,
                )
            ]
        )

    @pytest.mark.asyncio
    async def test_delete_port_forward_closes_server_and_removes_registry(self, mock_k8s_client, mock_workload_provider):
        service = PortForwardService(mock_k8s_client, "default", mock_workload_provider)
        mock_server = make_mock_server()

        with patch("src.services.port_forward_service.asyncio.start_server", new=AsyncMock(return_value=mock_server)):
            await service.create_port_forward("sandbox-test-123", 9090, 44772)

        await service.delete_port_forward("sandbox-test-123", 9090)

        mock_server.close.assert_called_once_with()
        mock_server.wait_closed.assert_awaited_once()
        assert service._registry == {}

    @pytest.mark.asyncio
    async def test_delete_port_forward_closes_active_connections(self, mock_k8s_client, mock_workload_provider):
        service = PortForwardService(mock_k8s_client, "default", mock_workload_provider)
        mock_server = make_mock_server()

        with patch("src.services.port_forward_service.asyncio.start_server", new=AsyncMock(return_value=mock_server)):
            await service.create_port_forward("sandbox-test-123", 9090, 44772)

        # Simulate two active connections in the state
        writer_a = MagicMock()
        writer_a.close = MagicMock()
        writer_a.wait_closed = AsyncMock()
        writer_b = MagicMock()
        writer_b.close = MagicMock()
        writer_b.wait_closed = AsyncMock()
        state = service._registry["sandbox-test-123"][9090]
        state.active_connections.update({writer_a, writer_b})

        await service.delete_port_forward("sandbox-test-123", 9090)

        # Both active connections must have been closed
        writer_a.close.assert_called_once_with()
        writer_b.close.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_cleanup_sandbox_removes_all_port_forwards_for_sandbox(self, mock_k8s_client, mock_workload_provider):
        service = PortForwardService(mock_k8s_client, "default", mock_workload_provider)
        server_one = make_mock_server()
        server_two = make_mock_server()

        with patch(
            "src.services.port_forward_service.asyncio.start_server",
            new=AsyncMock(side_effect=[server_one, server_two]),
        ):
            await service.create_port_forward("sandbox-test-123", 9090, 44772)
            await service.create_port_forward("sandbox-test-123", 9091, 8080)

        await service.cleanup_sandbox("sandbox-test-123")

        server_one.close.assert_called_once_with()
        server_two.close.assert_called_once_with()
        server_one.wait_closed.assert_awaited_once()
        server_two.wait_closed.assert_awaited_once()
        assert service._registry == {}

    @pytest.mark.asyncio
    async def test_cleanup_all_removes_all_port_forwards(self, mock_k8s_client, mock_workload_provider):
        service = PortForwardService(mock_k8s_client, "default", mock_workload_provider)
        servers = [make_mock_server() for _ in range(3)]

        with patch(
            "src.services.port_forward_service.asyncio.start_server",
            new=AsyncMock(side_effect=servers),
        ):
            await service.create_port_forward("sandbox-a", 9090, 44772)
            await service.create_port_forward("sandbox-a", 9091, 8080)
            await service.create_port_forward("sandbox-b", 9092, 8081)

        await service.cleanup_all()

        for server in servers:
            server.close.assert_called_once_with()
            server.wait_closed.assert_awaited_once()
        assert service._registry == {}

    @pytest.mark.asyncio
    async def test_create_port_forward_raises_404_when_sandbox_not_found(self, mock_k8s_client, mock_workload_provider):
        service = PortForwardService(mock_k8s_client, "default", mock_workload_provider)
        mock_workload_provider.get_workload.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            await service.create_port_forward("missing-sandbox", 9090, 44772)

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
        detail = cast(dict[str, Any], exc_info.value.detail)
        assert detail["code"] == SandboxErrorCodes.PORT_FORWARD_SANDBOX_NOT_FOUND

    @pytest.mark.asyncio
    async def test_create_port_forward_raises_conflict_when_port_already_exists(self, mock_k8s_client, mock_workload_provider):
        service = PortForwardService(mock_k8s_client, "default", mock_workload_provider)
        existing_server = make_mock_server()
        service._registry = {
            "sandbox-a": {
                9090: PortForwardState(
                    server=existing_server,
                    sandbox_id="sandbox-a",
                    local_port=9090,
                    remote_port=44772,
                    namespace="default",
                    pod_name="sandbox-a-pod",
                    created_at=datetime.now(timezone.utc),
                )
            }
        }

        with pytest.raises(HTTPException) as exc_info:
            await service.create_port_forward("sandbox-b", 9090, 8080)

        assert exc_info.value.status_code == status.HTTP_409_CONFLICT
        detail = cast(dict[str, Any], exc_info.value.detail)
        assert detail["code"] == SandboxErrorCodes.PORT_FORWARD_PORT_CONFLICT

    @pytest.mark.asyncio
    async def test_create_port_forward_wraps_bind_oserror(self, mock_k8s_client, mock_workload_provider):
        service = PortForwardService(mock_k8s_client, "default", mock_workload_provider)

        with patch(
            "src.services.port_forward_service.asyncio.start_server",
            new=AsyncMock(side_effect=OSError("address already in use")),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await service.create_port_forward("sandbox-test-123", 9090, 44772)

        assert exc_info.value.status_code == status.HTTP_409_CONFLICT
        detail = cast(dict[str, Any], exc_info.value.detail)
        assert detail["code"] == SandboxErrorCodes.PORT_FORWARD_PORT_CONFLICT

    @pytest.mark.asyncio
    async def test_create_port_forward_wraps_k8s_errors(self, mock_k8s_client, mock_workload_provider):
        service = PortForwardService(mock_k8s_client, "default", mock_workload_provider)
        mock_k8s_client.list_pods.side_effect = RuntimeError("k8s exploded")

        with pytest.raises(HTTPException) as exc_info:
            await service.create_port_forward("sandbox-test-123", 9090, 44772)

        assert exc_info.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        detail = cast(dict[str, Any], exc_info.value.detail)
        assert detail["code"] == SandboxErrorCodes.PORT_FORWARD_K8S_ERROR

    @pytest.mark.asyncio
    async def test_delete_port_forward_raises_not_found_for_unknown_port(self, mock_k8s_client, mock_workload_provider):
        service = PortForwardService(mock_k8s_client, "default", mock_workload_provider)

        with pytest.raises(HTTPException) as exc_info:
            await service.delete_port_forward("sandbox-test-123", 9090)

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
        detail = cast(dict[str, Any], exc_info.value.detail)
        assert detail["code"] == SandboxErrorCodes.PORT_FORWARD_NOT_FOUND

    @pytest.mark.asyncio
    async def test_handle_connection_opens_fresh_k8s_portforward_per_connection(
        self, mock_k8s_client, mock_workload_provider
    ):
        service = PortForwardService(mock_k8s_client, "default", mock_workload_provider)
        service._registry = {
            "sandbox-test-123": {
                9090: PortForwardState(
                    server=make_mock_server(),
                    sandbox_id="sandbox-test-123",
                    local_port=9090,
                    remote_port=44772,
                    namespace="default",
                    pod_name="sandbox-test-123-pod",
                    created_at=datetime.now(timezone.utc),
                )
            }
        }

        readers = []
        writers = []
        for _ in range(2):
            reader = AsyncMock()
            reader.read = AsyncMock(side_effect=[b""])
            writer = MagicMock()
            writer.is_closing.return_value = False
            writer.write = MagicMock()
            writer.drain = AsyncMock()
            writer.close = MagicMock()
            writer.wait_closed = AsyncMock()
            readers.append(reader)
            writers.append(writer)

        sockets = []
        port_forwards = []
        for _ in range(2):
            pf_socket = MagicMock()
            pf_socket.recv.return_value = b""
            pf_socket.sendall = MagicMock()
            pf_socket.close = MagicMock()
            pf = MagicMock()
            pf.socket.return_value = pf_socket
            pf.close = MagicMock()
            sockets.append(pf_socket)
            port_forwards.append(pf)

        async def run_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        with (
            patch("src.services.port_forward_service.asyncio.to_thread", side_effect=run_to_thread) as mock_to_thread,
            patch("src.services.port_forward_service.stream.portforward", side_effect=port_forwards) as mock_portforward,
        ):
            await service._handle_connection(readers[0], writers[0], "sandbox-test-123", 9090)
            await service._handle_connection(readers[1], writers[1], "sandbox-test-123", 9090)

        assert mock_portforward.call_count == 2
        assert mock_to_thread.await_count >= 2
        for writer in writers:
            writer.close.assert_called_once_with()
            writer.wait_closed.assert_awaited_once()
