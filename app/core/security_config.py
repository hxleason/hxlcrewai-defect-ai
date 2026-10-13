"""
app/core/security_config.py – 安全配置加载与校验（Phase 3 · v1.0 硬失败版）

设计原则：
    1. 硬失败（fail-closed）：
       安全关键配置缺失或非法 → 启动即抛 SecurityConfigError，进程拒绝启动。
       绝不允许"缺失配置 → 静默使用默认值"的失败开放。
    2. 安全侧默认（仅限功能开关）：
       开关缺失时采用更安全的一侧：失败关闭开、审计开、审核闸门开、
       表达式引擎关（safe_expr 尚未绑定，默认禁用动态表达式）。
       但开关一旦出现，必须是合法布尔值，非法值同样硬失败。
    3. 严格校验：
       枚举白名单、布尔白名单、整数范围、密钥长度分级（dev/test ≥16，prod ≥32）。
    4. 单次加载、进程内只读：
       load_security_config() 幂等，首次加载后缓存不可变快照。
    5. 可测试：
       load_security_config(env=dict) 可注入环境变量进行单元测试。

应用入口接线（app/__init__.py 或 app.py 最顶部）：
    from app.core.security_config import load_security_config, SecurityConfigError
    try:
        security = load_security_config()
    except SecurityConfigError as exc:
        import sys
        print(f"[FATAL] {exc}", file=sys.stderr)
        sys.exit(2)   # 硬失败：拒绝启动
"""

import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:  # python-dotenv 为可选依赖；未安装时直接读取 os.environ
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


class SecurityConfigError(RuntimeError):
    """安全配置缺失或非法。抛出即意味着应用必须拒绝启动（硬失败）。"""


_WEAK_SECRETS = {
    "secret", "changeme", "change-me", "password", "default",
    "dev-secret", "dev_secret", "123456", "abcdef",
}

_ALLOWED_ENVS = {"development", "testing", "production"}


@dataclass(frozen=True)
class SecurityConfig:
    """安全配置不可变快照。"""
    app_env: str
    secret_key: str
    audit_log_path: str
    audit_enabled: bool
    audit_max_entries: int
    fail_closed: bool
    review_gate_enabled: bool
    expr_engine_enabled: bool
    expr_max_length: int
    expr_max_depth: int


# ---------------------------------------------------------------------------
# 解析辅助（任何歧义一律记为错误，不猜测、不兜底）
# ---------------------------------------------------------------------------
def _get(
    env: Dict[str, str], key: str, errors: List[str],
    required: bool = True, default: Optional[str] = None,
) -> Optional[str]:
    raw = env.get(key)
    if raw is None or str(raw).strip() == "":
        if required:
            errors.append(f"缺少必需配置项: {key}")
        return default
    return str(raw).strip()


def _parse_bool(
    env: Dict[str, str], key: str, errors: List[str],
    default: Optional[bool] = None,
) -> Optional[bool]:
    raw = _get(env, key, errors, required=False)
    if raw is None:
        return default
    low = raw.lower()
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off"):
        return False
    errors.append(f"{key} 必须为布尔值（true/false/1/0），收到: {raw!r}")
    return None


def _parse_int(
    env: Dict[str, str], key: str, errors: List[str],
    default: Optional[int] = None,
    minimum: Optional[int] = None, maximum: Optional[int] = None,
) -> Optional[int]:
    raw = _get(env, key, errors, required=False)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        errors.append(f"{key} 必须为整数，收到: {raw!r}")
        return None
    if minimum is not None and value < minimum:
        errors.append(f"{key} 不得小于 {minimum}，收到: {value}")
    if maximum is not None and value > maximum:
        errors.append(f"{key} 不得大于 {maximum}，收到: {value}")
    return value


def _build(env: Dict[str, str]) -> SecurityConfig:
    """校验全部安全配置项。任何错误 → 汇总后一次抛出（硬失败）。"""
    errors: List[str] = []

    # ---- 必需项 ----
    app_env = _get(env, "APP_ENV", errors, required=True)
    if app_env and app_env not in _ALLOWED_ENVS:
        errors.append(f"APP_ENV 非法: {app_env!r}（允许值: {sorted(_ALLOWED_ENVS)}）")

    secret_key = _get(env, "SECRET_KEY", errors, required=True)
    if secret_key:
        min_len = 32 if app_env == "production" else 16
        if len(secret_key) < min_len:
            errors.append(
                f"SECRET_KEY 长度不足（当前 {len(secret_key)}，{app_env or '未知环境'} 要求 ≥{min_len}）"
            )
        if app_env == "production" and secret_key.lower() in _WEAK_SECRETS:
            errors.append("SECRET_KEY 命中已知弱密钥清单，生产环境拒绝使用")

    # ---- 功能开关（缺失 → 安全侧默认；非法 → 硬失败） ----
    fail_closed = _parse_bool(env, "FAIL_CLOSED", errors, default=True)
    audit_enabled = _parse_bool(env, "AUDIT_ENABLED", errors, default=True)
    review_gate_enabled = _parse_bool(env, "REVIEW_GATE_ENABLED", errors, default=True)
    expr_engine_enabled = _parse_bool(env, "EXPR_ENGINE_ENABLED", errors, default=False)

    # ---- 审计与表达式引擎参数 ----
    # 审计开启时路径必需（无默认路径，防止审计悄悄写进错误位置）
    audit_log_path = _get(env, "AUDIT_LOG_PATH", errors, required=bool(audit_enabled))
    audit_max_entries = _parse_int(
        env, "AUDIT_MAX_ENTRIES", errors, default=100000, minimum=1000
    )
    expr_max_length = _parse_int(
        env, "EXPR_MAX_LENGTH", errors, default=512, minimum=1, maximum=4096
    )
    expr_max_depth = _parse_int(
        env, "EXPR_MAX_DEPTH", errors, default=16, minimum=1, maximum=64
    )

    if errors:
        raise SecurityConfigError(
            "安全配置校验失败（硬失败，拒绝启动）:\n  - " + "\n  - ".join(errors)
        )

    return SecurityConfig(
        app_env=app_env,                        # type: ignore[arg-type]
        secret_key=secret_key,                  # type: ignore[arg-type]
        audit_log_path=audit_log_path or "",    # type: ignore[arg-type]
        audit_enabled=bool(audit_enabled),
        audit_max_entries=int(audit_max_entries),      # type: ignore[arg-type]
        fail_closed=bool(fail_closed),
        review_gate_enabled=bool(review_gate_enabled),
        expr_engine_enabled=bool(expr_engine_enabled),
        expr_max_length=int(expr_max_length),          # type: ignore[arg-type]
        expr_max_depth=int(expr_max_depth),            # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# 加载入口（幂等 + 缓存）
# ---------------------------------------------------------------------------
_loaded: Optional[SecurityConfig] = None
_load_lock = threading.Lock()


def load_security_config(
    env: Optional[Dict[str, str]] = None,
    dotenv_path: Optional[str] = None,
) -> SecurityConfig:
    """
    加载并校验安全配置（幂等）。

    - 生产路径（无参数）：load_dotenv（如可用）→ os.environ → 校验 → 缓存。
      校验失败抛 SecurityConfigError，应用必须据此拒绝启动。
    - 测试路径（显式传 env / dotenv_path）：不写缓存，用于单元测试。
    """
    global _loaded
    if env is None and dotenv_path is None:
        with _load_lock:
            if _loaded is not None:
                return _loaded
            if load_dotenv is not None:
                load_dotenv(override=False)  # 不覆盖进程已有环境变量
            cfg = _build(dict(os.environ))
            _loaded = cfg
            return cfg

    # 测试路径：显式环境注入
    if dotenv_path is not None and load_dotenv is not None:
        load_dotenv(dotenv_path=dotenv_path, override=False)
    source = dict(os.environ)
    if env is not None:
        source.update(env)
    return _build(source)


def get_security_config() -> SecurityConfig:
    """获取已加载的安全配置；未加载即抛错（强制应用入口显式初始化）。"""
    if _loaded is None:
        raise SecurityConfigError(
            "安全配置未加载：请在应用入口调用 load_security_config()"
        )
    return _loaded