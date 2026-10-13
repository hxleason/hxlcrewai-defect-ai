# -*- coding: utf-8 -*-
"""app 包导入闸门（Phase 3）：任何进程导入 app.* 时强制执行安全配置校验。

覆盖范围：
  - uvicorn app.main:app            → import app.main 先经过本文件
  - celery -A app.celery_app ...    → import app.celery_app 先经过本文件
  - pytest / 任意 CLI 脚本           → import app.* 先经过本文件

行为：配置缺失/非法 → 打印 [FATAL] 并以退出码 2 终止（硬失败，拒绝启动）。
"""
import sys

from app.core.security_config import load_security_config, SecurityConfigError

try:
    security = load_security_config()
except SecurityConfigError as exc:
    print(f"[FATAL] {exc}", file=sys.stderr)
    sys.exit(2)   # 硬失败：uvicorn / celery / pytest 统一以非零码退出

# 审计开关：AUDIT_ENABLED=true 才初始化（进程内幂等）
if security.audit_enabled:
    from app.core.audit import init_audit
    init_audit(security.audit_log_path, max_entries=security.audit_max_entries)
