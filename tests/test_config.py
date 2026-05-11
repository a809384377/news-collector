"""配置加载测试。"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from news_collector.config import (
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
    assert config.logging.file == "~/.news-collector/logs/news-collector.log"
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
