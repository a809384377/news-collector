"""``commands.setup`` 命令单测。

策略：
- mock ``docker_helpers`` 中三个函数（docker_daemon_alive / container_status /
  compose_up），避免测试时真启容器
- mock ``typer.prompt`` 控制 X token 引导分支
- 用 ``tmp_path`` 准备临时 home，验证目录树 / sources.yaml / .env / raw.db 落地

注：测试模块别名故意避开 ``setup_mod`` / ``teardown_module``（pytest xunit
hook 名字会被当成 hook 调用而触发 AttributeError——s3-cli-onboarding Step 3
subagent A 踩过此坑）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from news_collector.commands import setup as setup_mod  # NOTE: 别名故意避开
# `setup_module` —— pytest xunit setup_module hook 名字，会被当成 hook 调用。
from news_collector.commands.docker_helpers import DockerError


# ---- helpers ---------------------------------------------------------------


def _build_app() -> typer.Typer:
    """挂 setup_cmd + 一个 hidden 占位命令，防 typer 单命令扁平化。"""
    app = typer.Typer()
    app.command("setup")(setup_mod.setup_cmd)
    app.command("_placeholder", hidden=True)(lambda: None)
    return app


def _force_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制 _stdin_is_tty()=True，让 _guide_token 走交互 prompt 路径。

    CliRunner 默认 stdin 不是 tty，会触发非交互降级（直接 skip）；且 CliRunner
    会替换 sys.stdin，所以直接 patch sys.stdin.isatty 不生效——必须 patch
    setup.py 抽出的 ``_stdin_is_tty`` 函数。
    """
    monkeypatch.setattr(setup_mod, "_stdin_is_tty", lambda: True)


def _mock_docker_all_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """容器都 Up 的标准 mock。"""
    monkeypatch.setattr(setup_mod.docker_helpers, "docker_daemon_alive", lambda: True)
    monkeypatch.setattr(
        setup_mod.docker_helpers,
        "container_status",
        lambda *a, **kw: {"rsshub": "Up", "redis": "Up"},
    )
    monkeypatch.setattr(setup_mod.docker_helpers, "compose_up", lambda *a, **kw: None)


def _mock_docker_needs_up(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """容器未启 + compose_up 成功的 mock。返回 calls 列表用于断言 compose_up 被调用。"""
    calls: list[str] = []
    monkeypatch.setattr(setup_mod.docker_helpers, "docker_daemon_alive", lambda: True)
    monkeypatch.setattr(
        setup_mod.docker_helpers,
        "container_status",
        lambda *a, **kw: {"rsshub": "Exited", "redis": "Exited"},
    )

    def fake_up(*a, **kw):
        calls.append("compose_up")

    monkeypatch.setattr(setup_mod.docker_helpers, "compose_up", fake_up)
    return calls


# ---- 测试 ------------------------------------------------------------------


def test_setup_clean_home_full_do_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """干净 home + 用户输入 token，全 [do] 路径成功完工。"""
    home = tmp_path / "home"
    _mock_docker_needs_up(monkeypatch)
    _force_tty(monkeypatch)
    monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "fake-token-abc")

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["setup", "--home", str(home)])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "[do]   directories created" in out
    assert "[do]   sources.yaml seeded" in out
    assert "[do]   .env template written" in out
    assert "[do]   X auth_token saved" in out
    assert "[do]   docker compose up -d" in out
    assert "[do]   raw.db created" in out
    assert "Setup complete" in out

    # 实际副作用
    assert (home / "logs").is_dir()
    assert (home / "cache").is_dir()
    assert (home / "rsshub").is_dir()
    assert (home / "sources.yaml").exists()
    assert (home / ".env").exists()
    assert (home / "raw.db").exists()
    assert "TWITTER_AUTH_TOKEN=fake-token-abc" in (home / ".env").read_text()


def test_setup_idempotent_second_run_all_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一 home 跑两次：第二次全 [skip]，无副作用。"""
    home = tmp_path / "home"
    _mock_docker_all_up(monkeypatch)
    _force_tty(monkeypatch)
    monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "tk-1")

    runner = CliRunner()
    runner.invoke(_build_app(), ["setup", "--home", str(home)])
    result2 = runner.invoke(_build_app(), ["setup", "--home", str(home)])

    assert result2.exit_code == 0
    out = result2.output
    assert "[skip] directories already exist" in out
    assert "[skip] sources.yaml already exists" in out
    assert "[skip] .env already exists" in out
    assert "[skip] X auth_token already set" in out
    assert "[skip] containers already up" in out
    assert "[skip] raw.db already exists" in out


def test_setup_skips_token_when_user_blank_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户敲回车跳过 token → [warn] 提示 X 信源会失败但不阻塞 setup。"""
    home = tmp_path / "home"
    _mock_docker_needs_up(monkeypatch)
    monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "")

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["setup", "--home", str(home)])

    assert result.exit_code == 0, result.output
    assert "[warn] X auth_token left empty" in result.output
    # .env 仍是模板态（占位行 TWITTER_AUTH_TOKEN= 空）
    env_text = (home / ".env").read_text()
    assert "TWITTER_AUTH_TOKEN=\n" in env_text


def test_setup_recognizes_existing_token_in_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """.env 已有非空 token → 不再 prompt + 走 [skip] X auth_token already set。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("TWITTER_AUTH_TOKEN=preset-tk\n")

    _mock_docker_all_up(monkeypatch)

    # 用 prompt 抛错确保它不被调用
    def boom(*a, **kw):
        raise AssertionError("typer.prompt should not be called when token already set")

    monkeypatch.setattr(typer, "prompt", boom)

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["setup", "--home", str(home)])

    assert result.exit_code == 0, result.output
    assert "[skip] X auth_token already set" in result.output


def test_setup_fails_when_docker_daemon_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docker daemon 未启 → exit 1 + 错误提示。"""
    home = tmp_path / "home"
    monkeypatch.setattr(setup_mod.docker_helpers, "docker_daemon_alive", lambda: False)
    monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "")

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["setup", "--home", str(home)])

    assert result.exit_code == 1
    # stderr 在 typer.echo(..., err=True) 走的是 stderr；CliRunner 默认合并
    assert "docker step failed" in (result.output + (result.stderr or ""))


def test_setup_handles_compose_up_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compose_up 抛 DockerError → exit 1 + 错误提示。"""
    home = tmp_path / "home"
    monkeypatch.setattr(setup_mod.docker_helpers, "docker_daemon_alive", lambda: True)
    monkeypatch.setattr(
        setup_mod.docker_helpers,
        "container_status",
        lambda *a, **kw: {"rsshub": "Exited", "redis": "Exited"},
    )

    def fake_up(*a, **kw):
        raise DockerError("network create failed")

    monkeypatch.setattr(setup_mod.docker_helpers, "compose_up", fake_up)
    monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "")

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["setup", "--home", str(home)])

    assert result.exit_code == 1
    assert "docker step failed" in (result.output + (result.stderr or ""))
    assert "network create failed" in (result.output + (result.stderr or ""))


def test_setup_calls_compose_up_when_containers_not_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """容器 Exited → 实际调 compose_up，输出 [do] docker compose up -d。"""
    home = tmp_path / "home"
    calls = _mock_docker_needs_up(monkeypatch)
    monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "tk")

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["setup", "--home", str(home)])

    assert result.exit_code == 0
    assert "compose_up" in calls
    assert "[do]   docker compose up -d" in result.output


def test_setup_non_interactive_stdin_skips_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stdin 非 tty（CI / piped）→ 跳过 prompt + 输出 [warn]，不抛错。"""
    home = tmp_path / "home"
    _mock_docker_all_up(monkeypatch)
    # 不调 _force_tty，让 isatty 返回默认值（CliRunner 下为 False）

    # prompt 不应被调用
    def boom(*a, **kw):
        raise AssertionError("typer.prompt should not be called when stdin not tty")

    monkeypatch.setattr(typer, "prompt", boom)

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["setup", "--home", str(home)])

    assert result.exit_code == 0, result.output
    assert "[warn] X auth_token left empty" in result.output


def test_setup_token_write_replaces_existing_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """.env 已有空 TWITTER_AUTH_TOKEN= 行 + 其他行 → 替换不污染其他行。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        "OTHER_KEY=val1\nTWITTER_AUTH_TOKEN=\nANOTHER=val2\n"
    )
    _mock_docker_all_up(monkeypatch)
    _force_tty(monkeypatch)
    monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "new-token")

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["setup", "--home", str(home)])

    assert result.exit_code == 0
    env_text = (home / ".env").read_text()
    assert "TWITTER_AUTH_TOKEN=new-token" in env_text
    assert "OTHER_KEY=val1" in env_text
    assert "ANOTHER=val2" in env_text


def test_setup_copies_compose_yml_to_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """干净 home → docker-compose.yml 从 package data 拷贝到 home，内容含 RSSHub + Redis 配置。"""
    home = tmp_path / "home"
    _mock_docker_needs_up(monkeypatch)
    monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "")

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["setup", "--home", str(home)])

    assert result.exit_code == 0, result.output
    assert "[do]   docker-compose.yml copied" in result.output

    compose_path = home / "docker-compose.yml"
    assert compose_path.exists(), result.output
    compose_text = compose_path.read_text(encoding="utf-8")
    assert "services:" in compose_text
    assert "rsshub:" in compose_text
    assert "redis:" in compose_text
    assert "${HOME}/.news-collector/.env" in compose_text


def test_compose_yml_idempotent_on_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一 home 跑两次：第二次 docker-compose.yml [skip] 且文件未被覆盖（mtime + 内容不变）。"""
    home = tmp_path / "home"
    _mock_docker_all_up(monkeypatch)
    _force_tty(monkeypatch)
    monkeypatch.setattr(typer, "prompt", lambda *a, **kw: "tk-1")

    runner = CliRunner()
    runner.invoke(_build_app(), ["setup", "--home", str(home)])

    compose_path = home / "docker-compose.yml"
    assert compose_path.exists()
    mtime_before = compose_path.stat().st_mtime_ns
    content_before = compose_path.read_text(encoding="utf-8")

    result2 = runner.invoke(_build_app(), ["setup", "--home", str(home)])

    assert result2.exit_code == 0, result2.output
    assert "[skip] docker-compose.yml already exists" in result2.output
    assert compose_path.stat().st_mtime_ns == mtime_before
    assert compose_path.read_text(encoding="utf-8") == content_before
