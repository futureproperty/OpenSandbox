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

import pytest
from pydantic import ValidationError

from src.api.schema import CreatePortForwardRequest, PortForwardInfo, PortForwardListResponse
from src.services.constants import SandboxErrorCodes


class TestPortForwardSchemas:
    def test_create_request_valid(self):
        req = CreatePortForwardRequest(local_port=8080, remote_port=44772)
        assert req.local_port == 8080
        assert req.remote_port == 44772

    def test_create_request_via_alias(self):
        req = CreatePortForwardRequest(**{"localPort": 8080, "remotePort": 44772})
        assert req.local_port == 8080

    def test_invalid_port_zero(self):
        with pytest.raises(ValidationError):
            CreatePortForwardRequest(local_port=0, remote_port=44772)

    def test_invalid_port_too_large(self):
        with pytest.raises(ValidationError):
            CreatePortForwardRequest(local_port=70000, remote_port=44772)

    def test_invalid_port_negative(self):
        with pytest.raises(ValidationError):
            CreatePortForwardRequest(local_port=-1, remote_port=44772)

    def test_invalid_remote_port_zero(self):
        with pytest.raises(ValidationError):
            CreatePortForwardRequest(local_port=8080, remote_port=0)

    def test_port_boundary_min(self):
        req = CreatePortForwardRequest(local_port=1, remote_port=1)
        assert req.local_port == 1

    def test_port_boundary_max(self):
        req = CreatePortForwardRequest(local_port=65535, remote_port=65535)
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
