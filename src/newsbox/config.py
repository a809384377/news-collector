"""配置加载。

加载顺序：
1. 包内默认 _defaults/config.default.yaml
2. 用户 ~/.newsbox/config.yaml（deep merge 覆盖）
3. ~/.newsbox/.env（python-dotenv → Secrets）

注：采集层只承载采集相关配置（fetch / logging）。AI 模型 / 评分 / 聚类等消费方
配置由消费方各自管理（如 ~/.news-radar-ai/config.yaml）。
"""
from __future__ import annotations

import shutil
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, Field

DEFAULT_HOME = Path.home() / ".newsbox"

# .env 中识别的密钥环境变量名 → Secrets 字段名。
# 采集层暂无自身密钥；保留映射作为扩展点。
# 注：TWITTER_AUTH_TOKEN 由 RSSHub 容器读取，不经过本配置。
_SECRET_ENV_MAP: dict[str, str] = {}


class HttpRetryConfig(BaseModel):
    max_attempts: int
    backoff_base_seconds: int


class TwikitFetchConfig(BaseModel):
    """twikit adapter 专属配置（s10 新增）。

    twikit 不支持原生 since_id；adapter 内按 created_at 倒序翻页直到时间窗外，
    ``max_pages`` 是防止 since 写错时穷举翻页的硬上限（~5 页 × 40 条 = 200 条）。
    """

    max_pages: int = 5


class FetchConfig(BaseModel):
    default_since: str
    per_source_rate_limit_seconds: dict[str, int]
    concurrency: dict[str, int] = Field(default_factory=lambda: {"rss": 8, "web": 1})
    twikit: TwikitFetchConfig = Field(default_factory=TwikitFetchConfig)
    http_retry: HttpRetryConfig
    consecutive_failure_skip: int


class LoggingConfig(BaseModel):
    level: str
    file: str
    rotation: str
    retention_days: int


class ThresholdsConfig(BaseModel):
    """CLI 查询命令的软阻断阈值（s9 Step 3 / D4）。"""

    cli_read_warn: int = 10000


class Secrets(BaseModel):
    """从 .env 读取的密钥；当前采集层无字段。

    保留 get_raw 公共接口避免破坏潜在调用者；返回 None。
    """

    def get_raw(self, key: str) -> str | None:
        field = _SECRET_ENV_MAP.get(key)
        if field is None:
            return None
        return getattr(self, field, None)

    def __repr__(self) -> str:  # pragma: no cover
        return "Secrets()"

    def __str__(self) -> str:  # pragma: no cover
        return self.__repr__()


class AppConfig(BaseModel):
    fetch: FetchConfig
    logging: LoggingConfig
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    secrets: Secrets = Field(default_factory=Secrets)


# ---------- 内部工具 ----------

def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """深合并：override 覆盖 base。

    - 双方都是 dict → 递归合并
    - 否则 override 整体替换 base 对应键
    - base 中存在而 override 没有的键保留
    """
    result = dict(base)
    for key, ov_val in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(ov_val, dict)
        ):
            result[key] = _deep_merge(result[key], ov_val)
        else:
            result[key] = ov_val
    return result


def _load_default_yaml() -> dict[str, Any]:
    """从包内 _defaults/config.default.yaml 读默认。"""
    res = resource_files("newsbox._defaults") / "config.default.yaml"
    text = res.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("config.default.yaml 必须是 mapping")
    return data


def _default_yaml_path() -> Path:
    res = resource_files("newsbox._defaults") / "config.default.yaml"
    return Path(str(res))


# ---------- 公共接口 ----------

def load_config(home: Path = DEFAULT_HOME) -> AppConfig:
    """加载配置。

    步骤：
    1. 确保 home / 'logs' 与 home / 'cache' 目录存在
    2. 读包内默认 yaml 作为基础
    3. 若 home / 'config.yaml' 存在 → deep merge 覆盖
    4. 读 home / '.env'，装填 Secrets（当前 _SECRET_ENV_MAP 为空，跳过）
    5. 验证并返回 AppConfig
    """
    home = Path(home)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    (home / "cache").mkdir(parents=True, exist_ok=True)

    merged = _load_default_yaml()

    user_yaml = home / "config.yaml"
    if user_yaml.exists():
        with user_yaml.open("r", encoding="utf-8") as f:
            user_data = yaml.safe_load(f) or {}
        if not isinstance(user_data, dict):
            raise ValueError(f"{user_yaml} 必须是 mapping")
        merged = _deep_merge(merged, user_data)

    secrets_data: dict[str, str | None] = {}
    env_path = home / ".env"
    if env_path.exists() and _SECRET_ENV_MAP:
        env_values = dotenv_values(env_path)
        for env_key, field in _SECRET_ENV_MAP.items():
            val = env_values.get(env_key)
            if val is not None and val != "":
                secrets_data[field] = val

    secrets = Secrets(**secrets_data) if secrets_data else Secrets()
    return AppConfig(**merged, secrets=secrets)


def write_default_config(home: Path = DEFAULT_HOME, force: bool = False) -> Path:
    """把包内默认 yaml 拷贝到 home / 'config.yaml'。

    - home 不存在则创建
    - 目标存在且 force=False → FileExistsError
    返回写入的目标路径。
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    target = home / "config.yaml"
    if target.exists() and not force:
        raise FileExistsError(f"{target} 已存在；如需覆盖请传 force=True")

    src = _default_yaml_path()
    if src.is_file():
        shutil.copyfile(src, target)
    else:
        res = resource_files("newsbox._defaults") / "config.default.yaml"
        target.write_text(res.read_text(encoding="utf-8"), encoding="utf-8")
    return target
