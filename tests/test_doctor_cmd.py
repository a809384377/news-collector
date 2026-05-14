"""``commands.doctor`` 命令单测。

策略：
- mock docker_helpers.{docker_available, docker_daemon_alive, container_status}
- mock 抽样 adapter 的 fetch 方法（避免真出网）
- 用 ``tmp_path`` 准备临时 home + .env / sources.yaml / raw.db

注：测试模块别名故意避开 ``setup_module`` / ``teardown_module``（pytest xunit
hook 名字坑）。本文件用 ``doc_mod``。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from newsbox.commands import doctor as doc_mod
from newsbox.commands.docker_helpers import DockerError
from newsbox.db import init_db


# ---- helpers ---------------------------------------------------------------


def _build_app() -> typer.Typer:
    app = typer.Typer()
    app.command("doctor")(doc_mod.doctor_cmd)
    app.command("_placeholder", hidden=True)(lambda: None)
    return app


def _seed_healthy_home(home: Path) -> None:
    """干净 home + .env + sources.yaml + raw.db 全就绪。"""
    home.mkdir()
    (home / ".env").write_text("TWITTER_AUTH_TOKEN=ok-token-1234\n")
    (home / "sources.yaml").write_text(
        "rss:\n"
        "  - id: fake_rss\n"
        "    url: https://example.com/feed\n"
        "    tier: kol\n"
        "    domain: [ai]\n"
        "web:\n"
        "  - id: fake_web\n"
        "    url: https://example.com\n"
        "    selector: auto\n"
        "    tier: kol\n"
        "    domain: [ai]\n"
    )
    init_db(home / "raw.db")


def _mock_docker_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doc_mod.docker_helpers, "docker_available", lambda: True)
    monkeypatch.setattr(doc_mod.docker_helpers, "docker_daemon_alive", lambda: True)
    monkeypatch.setattr(
        doc_mod.docker_helpers,
        "container_status",
        lambda *a, **kw: {"rsshub": "Up", "redis": "Up"},
    )


def _mock_sample_adapters_ok(monkeypatch: pytest.MonkeyPatch, count: int = 3) -> None:
    """mock 两个 adapter 的 fetch 方法返回 N 条假文章。"""

    class _FakeAdapter:
        def __init__(self) -> None:
            pass

        async def fetch(self, source, since):
            return list(range(count))

    monkeypatch.setitem(doc_mod.ADAPTER_REGISTRY, "rss", _FakeAdapter)
    monkeypatch.setitem(doc_mod.ADAPTER_REGISTRY, "web", _FakeAdapter)


# ---- 测试 ------------------------------------------------------------------


def test_doctor_all_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全配置就绪 + adapter mock 成功 → exit 0 + 全 OK 输出。"""
    home = tmp_path / "home"
    _seed_healthy_home(home)
    _mock_docker_healthy(monkeypatch)
    _mock_sample_adapters_ok(monkeypatch, count=3)

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["doctor", "--home", str(home)])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "[OK]   docker daemon running" in out
    assert "[OK]   container rsshub = Up" in out
    assert "[OK]   container redis = Up" in out
    assert "[OK]   .env exists" in out
    assert "[OK]   TWITTER_AUTH_TOKEN set" in out
    assert "[OK]   sources.yaml exists" in out
    assert "raw.db migrations applied 3/3" in out  # s13 起含 0003_reddit_comments
    assert "[OK]   sample rss:fake_rss fetched 3 articles" in out
    assert "[OK]   sample web:fake_web fetched 3 articles" in out
    assert "doctor: OK" in out


def test_doctor_docker_daemon_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """daemon 不在跑 → [FAIL] + exit 1。"""
    home = tmp_path / "home"
    _seed_healthy_home(home)
    monkeypatch.setattr(doc_mod.docker_helpers, "docker_available", lambda: True)
    monkeypatch.setattr(doc_mod.docker_helpers, "docker_daemon_alive", lambda: False)
    _mock_sample_adapters_ok(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["doctor", "--home", str(home)])

    assert result.exit_code == 1
    assert "[FAIL] docker daemon 未运行" in result.output


def test_doctor_container_exited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rsshub Exited → [FAIL] + exit 1，但其他段仍打印。"""
    home = tmp_path / "home"
    _seed_healthy_home(home)
    monkeypatch.setattr(doc_mod.docker_helpers, "docker_available", lambda: True)
    monkeypatch.setattr(doc_mod.docker_helpers, "docker_daemon_alive", lambda: True)
    monkeypatch.setattr(
        doc_mod.docker_helpers,
        "container_status",
        lambda *a, **kw: {"rsshub": "Exited", "redis": "Up"},
    )
    _mock_sample_adapters_ok(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["doctor", "--home", str(home)])

    assert result.exit_code == 1
    assert "[FAIL] container rsshub = Exited" in result.output
    assert "[OK]   container redis = Up" in result.output


def test_doctor_token_empty_warns_but_not_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TWITTER_AUTH_TOKEN 空 → [WARN]，不 FAIL，exit 0。"""
    home = tmp_path / "home"
    _seed_healthy_home(home)
    (home / ".env").write_text("TWITTER_AUTH_TOKEN=\n")
    _mock_docker_healthy(monkeypatch)
    _mock_sample_adapters_ok(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["doctor", "--home", str(home)])

    assert result.exit_code == 0
    assert "[WARN] TWITTER_AUTH_TOKEN empty" in result.output
    assert "doctor: OK" in result.output


def test_doctor_db_missing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """raw.db 不存在 → [FAIL] + exit 1。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("TWITTER_AUTH_TOKEN=tk\n")
    (home / "sources.yaml").write_text("rss: []\nweb: []\n")
    _mock_docker_healthy(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["doctor", "--home", str(home)])

    assert result.exit_code == 1
    assert "[FAIL] raw.db not found" in result.output


def test_doctor_sample_fetch_error_warns_not_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """adapter.fetch 抛异常 → [WARN] 不 FAIL（KNOWLEDGE-LOG #7）。"""
    home = tmp_path / "home"
    _seed_healthy_home(home)
    _mock_docker_healthy(monkeypatch)

    class _BoomAdapter:
        async def fetch(self, source, since):
            raise RuntimeError("network broken")

    monkeypatch.setitem(doc_mod.ADAPTER_REGISTRY, "rss", _BoomAdapter)
    monkeypatch.setitem(doc_mod.ADAPTER_REGISTRY, "web", _BoomAdapter)

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["doctor", "--home", str(home)])

    assert result.exit_code == 0  # 网络抖动不应让 doctor FAIL
    assert "[WARN] sample" in result.output
    assert "network broken" in result.output


def test_doctor_sample_fetch_zero_articles_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """adapter 返回 0 articles → [WARN] 但不 FAIL（信源短期无更新非系统问题）。"""
    home = tmp_path / "home"
    _seed_healthy_home(home)
    _mock_docker_healthy(monkeypatch)
    _mock_sample_adapters_ok(monkeypatch, count=0)

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["doctor", "--home", str(home)])

    assert result.exit_code == 0
    assert "[WARN] sample rss:fake_rss fetched 0 articles" in result.output


def test_doctor_sample_fetch_timeout_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """adapter 超时 → [WARN]。"""
    home = tmp_path / "home"
    _seed_healthy_home(home)
    _mock_docker_healthy(monkeypatch)

    class _SlowAdapter:
        async def fetch(self, source, since):
            await asyncio.sleep(60)  # 远超 _SAMPLE_TIMEOUT_SECONDS

    monkeypatch.setitem(doc_mod.ADAPTER_REGISTRY, "rss", _SlowAdapter)
    monkeypatch.setitem(doc_mod.ADAPTER_REGISTRY, "web", _SlowAdapter)
    # 缩短超时让测试快
    monkeypatch.setattr(doc_mod, "_SAMPLE_TIMEOUT_SECONDS", 0.05)

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["doctor", "--home", str(home)])

    assert result.exit_code == 0
    assert "timeout" in result.output


def test_doctor_json_all_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--json 模式 + 全 OK：emit 单块 JSON，ok=true，checks 数组含各 section 名。"""
    home = tmp_path / "home"
    _seed_healthy_home(home)
    _mock_docker_healthy(monkeypatch)
    _mock_sample_adapters_ok(monkeypatch, count=3)

    runner = CliRunner()
    result = runner.invoke(
        _build_app(), ["doctor", "--home", str(home), "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["home"] == str(home)
    checks = payload["checks"]
    # 默认 fixture 不含 twikit 段 → twikit panel 整 panel skip，section 仅含四块
    section_names = {c["name"] for c in checks}
    assert section_names == {"docker", "config", "database", "sample_fetch"}
    # 全部 level 都该是 ok
    assert all(c["level"] == "ok" for c in checks), checks
    # 字段齐全
    for c in checks:
        assert "name" in c and "level" in c and "message" in c and "fix" in c


def test_doctor_json_db_missing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--json 模式 + raw.db 缺失：ok=false + exit 1，仍 emit 完整 JSON。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("TWITTER_AUTH_TOKEN=tk\n")
    (home / "sources.yaml").write_text("rss: []\nweb: []\n")
    _mock_docker_healthy(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        _build_app(), ["doctor", "--home", str(home), "--json"]
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    # 至少有一条 database fail，且 message 含 raw.db not found
    db_fails = [
        c for c in payload["checks"]
        if c["name"] == "database" and c["level"] == "fail"
    ]
    assert db_fails, payload["checks"]
    assert any("raw.db not found" in c["message"] for c in db_fails)


def test_doctor_docker_query_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """container_status 抛 DockerError → [FAIL] + exit 1。"""
    home = tmp_path / "home"
    _seed_healthy_home(home)
    monkeypatch.setattr(doc_mod.docker_helpers, "docker_available", lambda: True)
    monkeypatch.setattr(doc_mod.docker_helpers, "docker_daemon_alive", lambda: True)

    def boom(*a, **kw):
        raise DockerError("ps decode failure")

    monkeypatch.setattr(doc_mod.docker_helpers, "container_status", boom)
    _mock_sample_adapters_ok(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["doctor", "--home", str(home)])

    assert result.exit_code == 1
    assert "[FAIL]" in result.output
    assert "ps decode failure" in result.output


# ---- twikit panel 测试 -----------------------------------------------------


def _seed_home_with_twikit(home: Path, *, cookies_payload: str | None) -> None:
    """seed home，``sources.yaml`` 含一条 twikit 信源；``cookies_payload`` 为 None
    则不写 cookies 文件，否则按 raw 字符串写入。"""
    home.mkdir()
    (home / ".env").write_text("TWITTER_AUTH_TOKEN=ok-token-1234\n")
    (home / "sources.yaml").write_text(
        "rss:\n"
        "  - id: fake_rss\n"
        "    url: https://example.com/feed\n"
        "    tier: kol\n"
        "    domain: [ai]\n"
        "twikit:\n"
        "  - id: x_dotey\n"
        "    url: dotey\n"
        "    tier: kol\n"
        "    domain: [ai]\n"
    )
    init_db(home / "raw.db")
    if cookies_payload is not None:
        (home / "twikit_cookies.json").write_text(cookies_payload)


def _mock_sample_adapters_for_twikit_home(
    monkeypatch: pytest.MonkeyPatch, count: int = 1
) -> None:
    """mock rss + twikit adapter 都返回 N 条假数据（避免 sample_fetch 网络调用）。"""

    class _FakeAdapter:
        def __init__(self, *a, **kw) -> None:
            pass

        async def fetch(self, source, since):
            return list(range(count))

    monkeypatch.setitem(doc_mod.ADAPTER_REGISTRY, "rss", _FakeAdapter)
    monkeypatch.setitem(doc_mod.ADAPTER_REGISTRY, "web", _FakeAdapter)
    monkeypatch.setitem(doc_mod.ADAPTER_REGISTRY, "twikit", _FakeAdapter)


def test_doctor_twikit_panel_skipped_when_no_twikit_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认 fixture 仅 rss/web 段 → twikit panel 整 panel skip，输出无 ``[Twikit]`` header。"""
    home = tmp_path / "home"
    _seed_healthy_home(home)  # 无 twikit 段
    _mock_docker_healthy(monkeypatch)
    _mock_sample_adapters_ok(monkeypatch, count=1)

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["doctor", "--home", str(home)])

    assert result.exit_code == 0, result.output
    assert "[Twikit]" not in result.output
    assert "twikit_cookies.json" not in result.output


def test_doctor_twikit_cookies_missing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sources.yaml 含 twikit 段但 cookies 文件不存在 → twikit panel [FAIL] + exit 1。"""
    home = tmp_path / "home"
    _seed_home_with_twikit(home, cookies_payload=None)
    _mock_docker_healthy(monkeypatch)
    _mock_sample_adapters_for_twikit_home(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["doctor", "--home", str(home)])

    assert result.exit_code == 1
    assert "[Twikit]" in result.output
    assert "[OK]   twikit version" in result.output  # 版本号读取成功
    assert "[FAIL] twikit cookies 检查失败" in result.output
    assert "twikit_cookies.json" in result.output  # 文案带路径
    assert "docs/twikit-setup.md" in result.output  # fix 引用文档


def test_doctor_twikit_auth_token_placeholder_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cookies.json 中 auth_token 仍是 ``<placeholder>`` → [FAIL]，文案区分 auth_token。"""
    home = tmp_path / "home"
    _seed_home_with_twikit(
        home,
        cookies_payload=json.dumps({"auth_token": "<paste here>", "ct0": "abc123"}),
    )
    _mock_docker_healthy(monkeypatch)
    _mock_sample_adapters_for_twikit_home(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["doctor", "--home", str(home)])

    assert result.exit_code == 1
    assert "[FAIL] twikit cookies 检查失败" in result.output
    assert "auth_token" in result.output


def test_doctor_twikit_ct0_missing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cookies.json 缺 ct0 字段 → [FAIL]，文案区分 ct0。"""
    home = tmp_path / "home"
    _seed_home_with_twikit(
        home,
        cookies_payload=json.dumps({"auth_token": "real-token-value", "ct0": ""}),
    )
    _mock_docker_healthy(monkeypatch)
    _mock_sample_adapters_for_twikit_home(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["doctor", "--home", str(home)])

    assert result.exit_code == 1
    assert "[FAIL] twikit cookies 检查失败" in result.output
    assert "ct0" in result.output


def test_doctor_twikit_all_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cookies 完整 → twikit panel 三条 OK（版本 + cookies present + 字段就位）。"""
    home = tmp_path / "home"
    _seed_home_with_twikit(
        home,
        cookies_payload=json.dumps(
            {"auth_token": "real-token-value", "ct0": "real-ct0-hex"}
        ),
    )
    _mock_docker_healthy(monkeypatch)
    _mock_sample_adapters_for_twikit_home(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(_build_app(), ["doctor", "--home", str(home)])

    assert result.exit_code == 0, result.output
    assert "[Twikit]" in result.output
    assert "[OK]   twikit version" in result.output
    assert "[OK]   twikit_cookies.json present" in result.output
    assert "[OK]   auth_token + ct0 字段就位" in result.output


def test_doctor_twikit_json_section_included_when_triggered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--json 模式 + 触发 twikit panel：checks 数组含 name=twikit 条目。"""
    home = tmp_path / "home"
    _seed_home_with_twikit(
        home,
        cookies_payload=json.dumps(
            {"auth_token": "real-token-value", "ct0": "real-ct0-hex"}
        ),
    )
    _mock_docker_healthy(monkeypatch)
    _mock_sample_adapters_for_twikit_home(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        _build_app(), ["doctor", "--home", str(home), "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    section_names = {c["name"] for c in payload["checks"]}
    assert "twikit" in section_names
    twikit_checks = [c for c in payload["checks"] if c["name"] == "twikit"]
    assert all(c["level"] == "ok" for c in twikit_checks), twikit_checks
