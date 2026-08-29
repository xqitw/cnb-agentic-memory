from importlib.metadata import version

from cnb_agentic_memory import __version__


def test_version() -> None:
    """包内版本号与安装元数据（pyproject.toml）保持单一来源一致。"""
    assert __version__ == version("cnb-agentic-memory")
