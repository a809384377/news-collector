"""``docker_helpers`` 模块测试。

覆盖前置守卫两条分支（不依赖真实 docker daemon）：
1. docker CLI 不在 PATH → DockerError 文案含 "docker CLI 未安装"
2. compose_file 不存在 → DockerError 文案含引导：``news-collector setup``

s7-agent-skill-and-hotfix Step 1：v0.5.1 起 compose 文件移到 home 目录，
老用户从 v0.5.0 升级后会撞「docker-compose.yml 不存在」原始报错且无引导。
本测试锁定修复后的文案，防止后续修改回归丢失引导。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from news_collector.commands import docker_helpers
from news_collector.commands.docker_helpers import DockerError, _run_compose


def test_run_compose_raises_when_docker_cli_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """docker CLI 不在 PATH → DockerError + 明确文案。"""
    monkeypatch.setattr(docker_helpers, "docker_available", lambda: False)

    with pytest.raises(DockerError) as excinfo:
        _run_compose(
            ["up", "-d"],
            timeout=5.0,
            compose_file=tmp_path / "docker-compose.yml",
        )

    assert "docker CLI 未安装" in str(excinfo.value)


def test_run_compose_raises_when_compose_file_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """compose_file 不存在 → DockerError 文案附 setup 引导（升级踩坑修复）。"""
    monkeypatch.setattr(docker_helpers, "docker_available", lambda: True)
    missing = tmp_path / "docker-compose.yml"
    assert not missing.exists()

    with pytest.raises(DockerError) as excinfo:
        _run_compose(["ps"], timeout=5.0, compose_file=missing)

    msg = str(excinfo.value)
    assert "docker-compose.yml 不存在" in msg
    assert str(missing) in msg
    # 修复关键点：必须引导用户跑 setup 自动补齐
    assert "news-collector setup" in msg
