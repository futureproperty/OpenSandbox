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

from datetime import datetime, timezone

from fastapi import status
from fastapi.exceptions import HTTPException
from fastapi.testclient import TestClient

from src.api import lifecycle
from src.api import port_forward
from src.api.port_forward import port_forward_router
from src.api.schema import PortForwardInfo, PortForwardListResponse
from src.main import app
from src.services.constants import SandboxErrorCodes

# Register router for testing if not already present
_registered = {getattr(r, "path", None) for r in app.routes}
if "/sandboxes/{sandbox_id}/port-forwards" not in _registered:
    app.include_router(port_forward_router)
    app.include_router(port_forward_router, prefix="/v1")


def _make_info(sandbox_id="sbx-001", local_port=9090, remote_port=44772) -> PortForwardInfo:
    return PortForwardInfo(
        sandboxId=sandbox_id,
        localPort=local_port,
        remotePort=remote_port,
        createdAt=datetime.now(timezone.utc),
    )


class StubPortForwardService:
    async def create_port_forward(self, sandbox_id, local_port, remote_port):
        return _make_info(sandbox_id, local_port, remote_port)

    def list_port_forwards(self, sandbox_id):
        return PortForwardListResponse(portForwards=[_make_info(sandbox_id)])

    async def delete_port_forward(self, sandbox_id, local_port):
        return None


def test_create_port_forward_returns_201(
    client: TestClient,
    auth_headers: dict,
    monkeypatch,
) -> None:
    monkeypatch.setattr(port_forward, "port_forward_service", StubPortForwardService())

    resp = client.post(
        "/v1/sandboxes/sbx-001/port-forwards",
        headers=auth_headers,
        json={"localPort": 9090, "remotePort": 44772},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["localPort"] == 9090
    assert body["remotePort"] == 44772
    assert body["sandboxId"] == "sbx-001"
    assert "createdAt" in body


def test_list_port_forwards_returns_200(
    client: TestClient,
    auth_headers: dict,
    monkeypatch,
) -> None:
    monkeypatch.setattr(port_forward, "port_forward_service", StubPortForwardService())

    resp = client.get("/v1/sandboxes/sbx-001/port-forwards", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "portForwards" in body
    assert len(body["portForwards"]) == 1


def test_delete_port_forward_returns_204(
    client: TestClient,
    auth_headers: dict,
    monkeypatch,
) -> None:
    monkeypatch.setattr(port_forward, "port_forward_service", StubPortForwardService())

    resp = client.delete("/v1/sandboxes/sbx-001/port-forwards/9090", headers=auth_headers)
    assert resp.status_code == 204
    assert resp.text == ""


def test_not_supported_when_service_none(
    client: TestClient,
    auth_headers: dict,
    monkeypatch,
) -> None:
    monkeypatch.setattr(port_forward, "port_forward_service", None)

    resp = client.post(
        "/v1/sandboxes/sbx-001/port-forwards",
        headers=auth_headers,
        json={"localPort": 9090, "remotePort": 44772},
    )
    assert resp.status_code == 501
    body = resp.json()
    assert body["code"] == SandboxErrorCodes.PORT_FORWARD_NOT_SUPPORTED


def test_not_supported_list_when_service_none(
    client: TestClient,
    auth_headers: dict,
    monkeypatch,
) -> None:
    monkeypatch.setattr(port_forward, "port_forward_service", None)

    resp = client.get("/v1/sandboxes/sbx-001/port-forwards", headers=auth_headers)
    assert resp.status_code == 501


def test_not_supported_delete_when_service_none(
    client: TestClient,
    auth_headers: dict,
    monkeypatch,
) -> None:
    monkeypatch.setattr(port_forward, "port_forward_service", None)

    resp = client.delete("/v1/sandboxes/sbx-001/port-forwards/9090", headers=auth_headers)
    assert resp.status_code == 501


def test_port_conflict_returns_409(
    client: TestClient,
    auth_headers: dict,
    monkeypatch,
) -> None:
    class ConflictService:
        async def create_port_forward(self, sandbox_id, local_port, remote_port):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": SandboxErrorCodes.PORT_FORWARD_PORT_CONFLICT,
                    "message": "Port already in use",
                },
            )

    monkeypatch.setattr(port_forward, "port_forward_service", ConflictService())

    resp = client.post(
        "/v1/sandboxes/sbx-001/port-forwards",
        headers=auth_headers,
        json={"localPort": 9090, "remotePort": 44772},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == SandboxErrorCodes.PORT_FORWARD_PORT_CONFLICT


def test_sandbox_not_found_returns_404(
    client: TestClient,
    auth_headers: dict,
    monkeypatch,
) -> None:
    class NotFoundService:
        async def create_port_forward(self, sandbox_id, local_port, remote_port):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": SandboxErrorCodes.PORT_FORWARD_SANDBOX_NOT_FOUND,
                    "message": "Sandbox not found",
                },
            )

    monkeypatch.setattr(port_forward, "port_forward_service", NotFoundService())

    resp = client.post(
        "/v1/sandboxes/sbx-001/port-forwards",
        headers=auth_headers,
        json={"localPort": 9090, "remotePort": 44772},
    )
    assert resp.status_code == 404


def test_invalid_port_returns_422(
    client: TestClient,
    auth_headers: dict,
    monkeypatch,
) -> None:
    monkeypatch.setattr(port_forward, "port_forward_service", StubPortForwardService())

    resp = client.post(
        "/v1/sandboxes/sbx-001/port-forwards",
        headers=auth_headers,
        json={"localPort": 0, "remotePort": 44772},
    )
    assert resp.status_code == 422


def test_requires_auth(client: TestClient) -> None:
    resp = client.post(
        "/v1/sandboxes/sbx-001/port-forwards",
        json={"localPort": 9090, "remotePort": 44772},
    )
    assert resp.status_code == 401


def test_sandbox_deletion_cleans_port_forwards(
    client: TestClient,
    auth_headers: dict,
    monkeypatch,
) -> None:
    cleanup_calls: list = []

    class TrackingPortForwardService:
        async def cleanup_sandbox(self, sandbox_id: str) -> None:
            cleanup_calls.append(sandbox_id)

    class StubSandboxService:
        @staticmethod
        def delete_sandbox(sandbox_id: str) -> None:
            pass

    monkeypatch.setattr(port_forward, "port_forward_service", TrackingPortForwardService())
    monkeypatch.setattr(lifecycle, "sandbox_service", StubSandboxService())

    resp = client.delete("/v1/sandboxes/sbx-001", headers=auth_headers)
    assert resp.status_code == 204
    assert cleanup_calls == ["sbx-001"]


def test_sandbox_deletion_succeeds_even_if_cleanup_fails(
    client: TestClient,
    auth_headers: dict,
    monkeypatch,
) -> None:
    class FailingPortForwardService:
        async def cleanup_sandbox(self, sandbox_id: str) -> None:
            raise RuntimeError("cleanup failed")

    class StubSandboxService:
        @staticmethod
        def delete_sandbox(sandbox_id: str) -> None:
            pass

    monkeypatch.setattr(port_forward, "port_forward_service", FailingPortForwardService())
    monkeypatch.setattr(lifecycle, "sandbox_service", StubSandboxService())

    resp = client.delete("/v1/sandboxes/sbx-001", headers=auth_headers)
    assert resp.status_code == 204
