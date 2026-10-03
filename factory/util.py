from __future__ import annotations
import contextlib
import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

class FactoryError(RuntimeError):
    """An actionable, safe-to-print application error."""

class UnknownWrite(FactoryError):
    """A remote mutation may have succeeded. Reconcile; do not blindly retry."""

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()

def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def larkcli_tempdir(prefix: str):
    """Create a temporary directory accepted by lark-cli's local-path guard."""
    root = Path("/tmp")
    if not root.is_dir():
        raise FactoryError("lark-cli 安全下载需要可用的 /tmp 临时目录。")
    return tempfile.TemporaryDirectory(prefix=prefix, dir=str(root))

def read_json(path: Path) -> Any:
    if path.stat().st_size > 32 * 1024 * 1024:
        raise FactoryError("JSON 文件超过本工具 32MB 限制。")
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (ValueError, UnicodeError) as exc:
        raise FactoryError(f"JSON 无效：{path.name}") from exc

def write_json(path: Path, value: Any) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode())

def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def safe_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise FactoryError("资源 ID 只能包含字母、数字、下划线和连字符。")
    return value

def confined(root: Path, relative: str, must_exist: bool = True) -> Path:
    """Reject absolute paths, traversal, and symlink escapes, including a symlinked parent."""
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts or "\\" in relative or "\x00" in relative:
        raise FactoryError("文件路径越界。")
    root = root.resolve()
    candidate = (root / raw).resolve()
    if candidate == root or not candidate.is_relative_to(root):
        raise FactoryError("文件路径越界。")
    cursor = root
    for part in raw.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise FactoryError("执行目录内不接受软链接。")
    if must_exist and not candidate.is_file():
        raise FactoryError(f"文件不存在：{relative}")
    return candidate

def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return "".join(value)
    if isinstance(value, list) and all(isinstance(x, dict) and "text" in x for x in value):
        return "".join(str(x["text"]) for x in value)
    raise FactoryError("应为文本字段；请运行 schema-check 检查字段类型。")

def links(value: Any) -> list[str]:
    if not value:
        return []
    if not isinstance(value, list):
        raise FactoryError("关联字段应为记录 ID 数组。")
    result = []
    for item in value:
        if isinstance(item, str):
            result.append(safe_id(item))
        elif isinstance(item, dict) and (item.get("record_id") or item.get("id")):
            result.append(safe_id(item.get("record_id", item.get("id"))))
        else:
            raise FactoryError("关联字段结构不兼容，禁止猜测关联对象。")
    return result

def integer(value: Any, label: str, low: int = 0, high: int = 1_000_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or int(value) != value or not low <= value <= high:
        raise FactoryError(f"{label} 必须是 {low} 至 {high} 的整数。")
    return int(value)

@contextlib.contextmanager
def process_lock(root: Path):
    """Single-host advisory lock. No claim of distributed exclusivity."""
    import fcntl
    root.mkdir(parents=True, exist_ok=True)
    with (root / "worker.lock").open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FactoryError("已有脚本正在运行。当前版本只支持一台电脑、一个执行器。") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
