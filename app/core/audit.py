"""
app/core/audit.py – 审计轨迹（Phase 3 · v1.0 防篡改哈希链版）

设计目标：让每一次 FMEA 评估都可追溯、可验真。

特性：
    1. 追加写 JSONL（一行一条），带自增序号 / 时间戳 / 事件类型 / 载荷。
    2. 防篡改哈希链：
       chain_hash = SHA-256(prev_hash ‖ canonical_json(entry))
       任一记录的插入 / 删除 / 修改都会导致其后所有链值断裂。
    3. sidecar 元数据（.meta）原子更新（临时文件 + os.replace + fsync），
       启动时 O(1) 恢复链尾；元数据缺失时全量扫描重建。
    4. verify() 全量重算校验，返回违规明细；启动期发现断裂 → 硬失败。
    5. 线程安全：写路径全程持锁。
    6. 失败关闭：日志写失败不静默（record 抛 AuditWriteError），
       由调用方（utils.fmea_calculator）强制 review_required=True。

使用方式：
    # 应用入口（仅当安全配置 AUDIT_ENABLED=true 时）
    from app.core.audit import init_audit
    init_audit("logs/audit.jsonl", max_entries=100000)

    # 评估留痕（utils 内部调用；审计未初始化时为无操作）
    from app.core.audit import record_assessment
    record_assessment(defect, result)

    # 完整性校验（运维巡检）
    ok, issues = get_audit().verify()
"""

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("defect_fmea.audit")

_GENESIS_HASH = "0" * 64


class AuditError(Exception):
    """审计模块基础异常。"""


class AuditWriteError(AuditError):
    """审计写入失败（调用方应执行失败关闭）。"""


class AuditIntegrityError(AuditError):
    """审计日志完整性校验失败（硬失败，需人工处置）。"""


def _canonical_json(obj: Any) -> str:
    """规范化 JSON：键排序、无空格，保证同一内容永远产生同一字节串。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _chain_hash(prev_hash: str, entry: Dict[str, Any]) -> str:
    """哈希链计算：SHA-256(前一条链值 ‖ 本条规范化 JSON)。"""
    payload = prev_hash + "\u2016" + _canonical_json(entry)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditTrail:
    """追加写、防篡改的审计轨迹。"""

    def __init__(self, log_path: str, max_entries: Optional[int] = None):
        self.log_path = os.path.abspath(log_path)
        self.meta_path = self.log_path + ".meta"
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._last_seq = 0
        self._last_hash: Optional[str] = None
        self._bootstrap()

    # ------------------- 启动引导 -------------------
    def _bootstrap(self) -> None:
        parent = os.path.dirname(self.log_path)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                raise AuditError(f"审计日志目录创建失败: {parent}: {e}") from e

        meta = self._read_meta()
        if meta is not None:
            # sidecar 正常：O(1) 恢复链尾
            self._last_seq = int(meta["last_seq"])
            self._last_hash = str(meta["last_hash"])
            return

        # sidecar 缺失：全量扫描重建（升级场景 / sidecar 被误删）
        if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > 0:
            ok, seq, last_hash, issues = self._scan()
            if not ok:
                raise AuditIntegrityError(
                    "审计日志完整性校验失败（硬失败，请人工处置）:\n  - "
                    + "\n  - ".join(issues[:5])
                )
            self._last_seq, self._last_hash = seq, last_hash
            self._write_meta()
            logger.info("审计日志缺少元数据，已全量重建：seq=%s", seq)
        # 全新日志：从创世哈希开始

    # ------------------- 元数据 sidecar -------------------
    def _read_meta(self) -> Optional[Dict[str, Any]]:
        if not os.path.exists(self.meta_path):
            return None
        try:
            with open(self.meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if not isinstance(meta, dict) or "last_seq" not in meta or "last_hash" not in meta:
                raise ValueError("元数据字段缺失")
            return meta
        except Exception as e:
            logger.warning("审计元数据读取失败，将全量重建: %s", e)
            return None

    def _write_meta(self) -> None:
        """原子更新元数据。失败不抛——日志本体已落盘，下次启动可全量重建。"""
        meta = {"last_seq": self._last_seq, "last_hash": self._last_hash}
        tmp_path = self.meta_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.meta_path)
        except OSError as e:
            logger.error("审计元数据写入失败（日志已落盘，下次启动将全量重建）: %s", e)

    # ------------------- 全量扫描 / 校验 -------------------
    def _scan(self) -> Tuple[bool, int, Optional[str], List[str]]:
        """逐条重算哈希链。返回 (是否完整, 末序号, 末链值, 违规明细)。"""
        prev_hash = _GENESIS_HASH
        seq = 0
        last_hash: Optional[str] = None
        issues: List[str] = []
        with open(self.log_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as e:
                    issues.append(f"第 {line_no} 行 JSON 解析失败: {e}")
                    continue
                stored_prev = entry.get("prev_hash")
                stored_chain = entry.get("chain_hash")
                stripped = {k: v for k, v in entry.items()
                            if k not in ("chain_hash", "prev_hash")}
                recomputed = _chain_hash(
                    stored_prev if isinstance(stored_prev, str) else "", stripped
                )
                if stored_prev != prev_hash or stored_chain != recomputed:
                    issues.append(
                        f"第 {line_no} 行哈希链断裂（seq={entry.get('seq')}）"
                    )
                prev_hash = recomputed
                if isinstance(entry.get("seq"), int):
                    seq = entry["seq"]
                last_hash = recomputed
        return (not issues), seq, last_hash, issues

    def verify(self) -> Tuple[bool, List[str]]:
        """完整性校验：全量重算哈希链，并核对 sidecar 元数据。"""
        with self._lock:
            ok, seq, last_hash, issues = self._scan()
            if issues:
                return False, issues
            meta = self._read_meta()
            if meta is not None and (
                int(meta.get("last_seq")) != seq
                or str(meta.get("last_hash")) != last_hash
            ):
                issues.append("审计元数据（.meta）与日志尾部不一致")
            return not issues, issues

    # ------------------- 写入 -------------------
    def record(self, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        追加一条审计记录（线程安全）。
        写入失败抛 AuditWriteError —— 调用方必须失败关闭，绝不静默。
        """
        with self._lock:
            base = {
                "seq": self._last_seq + 1,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                "event": event_type,
                "data": payload,
            }
            prev_hash = self._last_hash or _GENESIS_HASH
            entry = dict(base)
            entry["prev_hash"] = prev_hash
            entry["chain_hash"] = _chain_hash(prev_hash, base)

            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(_canonical_json(entry) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            except OSError as e:
                raise AuditWriteError(f"审计日志写入失败: {e}") from e

            self._last_seq = entry["seq"]
            self._last_hash = entry["chain_hash"]
            self._write_meta()

            if self.max_entries and self._last_seq >= self.max_entries:
                logger.warning(
                    "审计日志条数已达上限 %s，请执行归档轮转（校验后转存，并同步重置 .meta）",
                    self.max_entries,
                )
            return entry

    # ------------------- 查询 -------------------
    def search(
        self, event_type: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """按事件类型检索最近 limit 条记录（审计回溯用）。"""
        results: List[Dict[str, Any]] = []
        if not os.path.exists(self.log_path):
            return results
        with self._lock:
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event_type is not None and entry.get("event") != event_type:
                        continue
                    results.append(entry)
        return results[-limit:]


# ---------------------------------------------------------------------------
# 进程级单例 + 便捷入口
# ---------------------------------------------------------------------------
_default_trail: Optional[AuditTrail] = None
_default_lock = threading.Lock()


def init_audit(log_path: str, max_entries: Optional[int] = None) -> AuditTrail:
    """初始化全局审计实例（应用入口调用；由 AUDIT_ENABLED 开关决定是否调用）。"""
    global _default_trail
    with _default_lock:
        _default_trail = AuditTrail(log_path, max_entries=max_entries)
        logger.info("审计轨迹已初始化: %s", log_path)
        return _default_trail


def get_audit() -> Optional[AuditTrail]:
    """获取全局审计实例；未初始化返回 None（表示审计未启用）。"""
    return _default_trail


def _sanitize(obj: Any, depth: int = 0) -> Any:
    """载荷净化：将不可 JSON 序列化的对象（如 Ellipsis、自定义类型）转为安全表示。"""
    if depth > 8:
        return "<max-depth>"
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if obj is Ellipsis:
        return "<ellipsis>"
    if isinstance(obj, dict):
        return {str(k): _sanitize(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_sanitize(v, depth + 1) for v in obj]
    return str(obj)


def _summarize(result: Dict[str, Any]) -> Dict[str, Any]:
    """数据最小化：只留痕评估关键字段，剔除大体积相似案例明细。"""
    keys = [
        "error", "message", "warning", "review_required", "review_reasons",
        "rule_failures", "severity", "occurrence", "detection", "rpn",
        "risk_level", "level", "standard_ref", "triggered_rules",
        "rule_applications", "similar_case_ids", "base_source", "source_case",
    ]
    return {k: _sanitize(result.get(k)) for k in keys if k in result}


def record_assessment(defect: Dict[str, Any], result: Dict[str, Any]) -> None:
    """
    评估留痕（fmea_calculator 集成点）。

    - 审计未启用（init_audit 未调用）→ 无操作，不阻塞评估。
    - 审计已启用但写入失败 → 抛 AuditWriteError，由调用方失败关闭。
    """
    trail = _default_trail
    if trail is None:
        return
    trail.record("fmea.evaluate", {
        "defect": _sanitize(defect),
        "result": _summarize(result),
    })