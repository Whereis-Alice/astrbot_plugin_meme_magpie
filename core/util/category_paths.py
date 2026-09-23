"""Portable category directory boundaries, including existing Chinese keys."""

from pathlib import Path
import re
import unicodedata


_RESERVED = {"con", "prn", "aux", "nul", "clock$", "conin$", "conout$"}
_RESERVED.update(f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10))
_RESERVED.update(f"{prefix}{number}" for prefix in ("com", "lpt") for number in "¹²³")


def validate_category_key(value: object) -> str:
    """Validate without renaming existing keys or changing their case."""
    key = str(value or "")
    if not key or len(key) > 100 or key != key.strip() or key.endswith("."):
        raise ValueError("分类 key 不能为空、超过100字或以空白/句点结尾")
    if key in {".", ".."} or re.search(r'[<>:"/\\|?*]', key):
        raise ValueError("分类 key 必须是单个目录名，不能包含路径分隔符或非法字符")
    if any(unicodedata.category(char).startswith("C") for char in key):
        raise ValueError("分类 key 不能包含控制字符")
    if key.split(".", 1)[0].rstrip(" ").casefold() in _RESERVED:
        raise ValueError("分类 key 不能使用系统保留名称")
    return key


def category_path(root: Path, key: object) -> Path:
    """Return a checked path without creating directories or moving files."""
    target = Path(root) / validate_category_key(key)
    if target.is_symlink() or (getattr(target, "is_junction", lambda: False)()):
        raise ValueError("分类目录不能是符号链接或目录联接")
    if target.resolve().parent != Path(root).resolve():
        raise ValueError("分类目录超出允许范围")
    return target
