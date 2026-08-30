"""MCP 独立入口单元测试：未安装 [mcp] extra 时的友好错误与退出码。"""

from __future__ import annotations

import builtins

import pytest

from cnb_agentic_memory import mcp_main


def test_missing_extra_friendly_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """未安装 [mcp] extra 时给出安装指引并以退出码 2 退出（非裸 traceback）。"""
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object):
        if name.endswith("mcp_server"):
            raise ImportError("No module named 'mcp'")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(SystemExit) as exc_info:
        mcp_main.main()
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "缺少 MCP 依赖" in captured.err
    assert 'pip install "cnb-agentic-memory[mcp]"' in captured.err
