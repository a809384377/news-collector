"""配置加载测试 + ``newsbox config`` 子命令测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from newsbox.commands.config import app as config_app
from newsbox.config import (
    AppConfig,
    Secrets,
    load_config,
    write_default_config,
)


def test_load_config_pure_default(tmp_path: Path) -> None:
    """空 home 加载 → 返回默认 AppConfig；logs/ 与 cache/ 被创建。"""
    config = load_config(home=tmp_path)
    assert isinstance(config, AppConfig)

    # logs 与 cache 目录被创建
    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / "cache").is_dir()

    # fetch / logging 字段与 _defaults/config.default.yaml 一致
    assert config.fetch.default_since == "24h"
    assert config.fetch.per_source_rate_limit_seconds == {
        "rss": 1,
        "web": 2,
    }
    assert config.fetch.http_retry.max_attempts == 4
    assert config.fetch.http_retry.backoff_base_seconds == 1
    assert config.fetch.consecutive_failure_skip == 3

    assert config.logging.level == "info"
    assert config.logging.file == "~/.newsbox/logs/newsbox.log"
    assert config.logging.rotation == "daily"
    assert config.logging.retention_days == 30

    # 采集层无密钥字段
    assert isinstance(config.secrets, Secrets)


def test_load_config_user_override_partial(tmp_path: Path) -> None:
    """用户覆盖 fetch.default_since，其他字段保留默认。"""
    user_yaml = tmp_path / "config.yaml"
    user_yaml.write_text(
        "fetch:\n  default_since: 7d\n",
        encoding="utf-8",
    )

    config = load_config(home=tmp_path)

    # 用户覆盖字段生效
    assert config.fetch.default_since == "7d"
    # 同模块未指定字段保留默认
    assert config.fetch.consecutive_failure_skip == 3
    assert config.fetch.http_retry.max_attempts == 4


def test_load_config_user_override_nested_dict(tmp_path: Path) -> None:
    """用户只覆盖 per_source_rate_limit_seconds 中一个 source_type，其他保留。"""
    user_yaml = tmp_path / "config.yaml"
    user_yaml.write_text(
        "fetch:\n"
        "  per_source_rate_limit_seconds:\n"
        "    web: 5\n",
        encoding="utf-8",
    )

    config = load_config(home=tmp_path)
    assert config.fetch.per_source_rate_limit_seconds == {
        "rss": 1,
        "web": 5,  # 被覆盖
    }
    # 兄弟字段未动
    assert config.fetch.default_since == "24h"


def test_secrets_get_raw_returns_none(tmp_path: Path) -> None:
    """采集层无密钥字段，get_raw 永远返回 None（兼容性接口）。"""
    config = load_config(home=tmp_path)
    assert config.secrets.get_raw("ANY_KEY") is None
    assert repr(config.secrets) == "Secrets()"


def test_write_default_config(tmp_path: Path) -> None:
    """write_default_config 把默认模板拷到 tmp_path/config.yaml；二次调用拒绝。"""
    target = write_default_config(home=tmp_path)

    assert target == tmp_path / "config.yaml"
    assert target.is_file()

    # 内容能被 yaml 解析
    with target.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict)
    assert "fetch" in data
    assert "logging" in data
    assert data["fetch"]["default_since"] == "24h"

    # 不带 force 二次调用应抛 FileExistsError
    with pytest.raises(FileExistsError):
        write_default_config(home=tmp_path)

    # force=True 可覆盖
    target2 = write_default_config(home=tmp_path, force=True)
    assert target2 == target


def test_write_default_config_creates_home(tmp_path: Path) -> None:
    """home 不存在时 write_default_config 应创建。"""
    sub = tmp_path / "nested" / "home"
    target = write_default_config(home=sub)
    assert sub.is_dir()
    assert target.is_file()


# ---- ``newsbox config`` --json CLI 测试（s9 Step 2） -----------------------


def _run(*args: str) -> "object":
    """走 typer.Typer app（config 子组），返回 CliRunner result。"""
    runner = CliRunner()
    return runner.invoke(config_app, list(args))


def test_config_init_human_view(tmp_path: Path) -> None:
    """init 人类视图：[ok] 行不变，路径出现在 stdout。"""
    home = tmp_path / "home"
    result = _run("init", "--home", str(home))
    assert result.exit_code == 0
    assert "[ok] config initialized:" in result.output
    assert str(home / "config.yaml") in result.output


def test_config_init_json_ok(tmp_path: Path) -> None:
    """init --json happy path：{ok: true, details.path, details.already_exists=False}。"""
    home = tmp_path / "home"
    result = _run("init", "--home", str(home), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["message"] == "config initialized"
    assert payload["details"]["home"] == str(home)
    assert payload["details"]["path"] == str(home / "config.yaml")
    assert payload["details"]["already_exists"] is False
    assert (home / "config.yaml").is_file()


def test_config_init_json_already_exists(tmp_path: Path) -> None:
    """init --json 已存在路径：{ok: false, details.already_exists=True}，exit 1。"""
    home = tmp_path / "home"
    # 先写一遍
    first = _run("init", "--home", str(home))
    assert first.exit_code == 0
    # 不带 --force 再调一遍
    result = _run("init", "--home", str(home), "--json")
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["details"]["already_exists"] is True
    assert payload["details"]["path"] == str(home / "config.yaml")


def test_config_init_json_force_overwrites(tmp_path: Path) -> None:
    """init --json --force：已存在文件也能成功写入。"""
    home = tmp_path / "home"
    _run("init", "--home", str(home))
    result = _run("init", "--home", str(home), "--force", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["details"]["already_exists"] is False


def test_config_show_human_view(tmp_path: Path) -> None:
    """show 人类视图：两行（JSON dump + secrets repr）保留。"""
    home = tmp_path / "home"
    home.mkdir()
    result = _run("show", "--home", str(home))
    assert result.exit_code == 0
    out = result.output
    # 第一行是 JSON dump（fetch / logging 块）
    assert '"fetch"' in out
    assert '"logging"' in out
    # 第二行是 secrets repr
    assert "secrets: Secrets()" in out


def test_config_show_json(tmp_path: Path) -> None:
    """show --json：返回 {home, path, config: {...}, secrets: '...'}。"""
    home = tmp_path / "home"
    home.mkdir()
    result = _run("show", "--home", str(home), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["home"] == str(home)
    assert payload["path"] == str(home / "config.yaml")
    # 配置 dump 含 fetch / logging 子树
    assert "fetch" in payload["config"]
    assert "logging" in payload["config"]
    assert payload["config"]["fetch"]["default_since"] == "24h"
    # secrets 以 repr 字符串呈现（脱敏）
    assert payload["secrets"] == "Secrets()"
