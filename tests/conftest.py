"""测试公共夹具：respx mock CNB API，不依赖真实网络。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from cam import CNBApiClient


@pytest.fixture
async def client() -> AsyncIterator[CNBApiClient]:
    """带假 token/repo 的客户端（所有请求由 respx 拦截）。"""
    async with CNBApiClient(token="test-token", repo="group/repo") as client:
        yield client
