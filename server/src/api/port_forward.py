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

from typing import Optional

from fastapi import APIRouter, Header, status
from fastapi.exceptions import HTTPException
from fastapi.responses import Response

from src.api.schema import (
    CreatePortForwardRequest,
    ErrorResponse,
    PortForwardInfo,
    PortForwardListResponse,
)
from src.services.constants import SandboxErrorCodes
from src.services.port_forward_service import PortForwardService

port_forward_router = APIRouter(tags=["Sandboxes"])

port_forward_service: Optional[PortForwardService] = None


def _require_service() -> PortForwardService:
    if port_forward_service is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={
                "code": SandboxErrorCodes.PORT_FORWARD_NOT_SUPPORTED,
                "message": "Port-forward is not supported for this runtime",
            },
        )
    return port_forward_service


@port_forward_router.post(
    "/sandboxes/{sandbox_id}/port-forwards",
    response_model=PortForwardInfo,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Port-forward created"},
        400: {"model": ErrorResponse, "description": "Invalid request"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "Sandbox not found"},
        409: {"model": ErrorResponse, "description": "Port conflict"},
        501: {"model": ErrorResponse, "description": "Not supported for this runtime"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def create_port_forward(
    sandbox_id: str,
    request: CreatePortForwardRequest,
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID"),
) -> PortForwardInfo:
    svc = _require_service()
    return await svc.create_port_forward(sandbox_id, request.local_port, request.remote_port)


@port_forward_router.get(
    "/sandboxes/{sandbox_id}/port-forwards",
    response_model=PortForwardListResponse,
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Port-forwards listed"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "Sandbox not found"},
        501: {"model": ErrorResponse, "description": "Not supported for this runtime"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def list_port_forwards(
    sandbox_id: str,
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID"),
) -> PortForwardListResponse:
    svc = _require_service()
    return svc.list_port_forwards(sandbox_id)


@port_forward_router.delete(
    "/sandboxes/{sandbox_id}/port-forwards/{local_port}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Port-forward deleted"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "Port-forward not found"},
        501: {"model": ErrorResponse, "description": "Not supported for this runtime"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def delete_port_forward(
    sandbox_id: str,
    local_port: int,
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID"),
) -> Response:
    svc = _require_service()
    await svc.delete_port_forward(sandbox_id, local_port)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
