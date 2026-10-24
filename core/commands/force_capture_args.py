"""Parse optional role/work labels for the administrator's capture window."""

import re
import shlex


_COMMAND = re.compile(r"(?<!\w)(?:mp|magpie|神偷)\s+偷(?:\s+|$)")
_LABELS = {"角色": "character", "character": "character", "作品": "work", "work": "work"}


def parse_force_capture_args(
    character: str = "", work: str = "", *, message: str = ""
) -> tuple[str, str]:
    """Accept positional labels or named labels; quoted names may contain spaces.

    AstrBot command filters split on spaces before binding optional parameters.
    Prefer the original command tail when available so quoted labels and extra
    arguments are parsed completely instead of silently truncated.
    """
    match = _COMMAND.search(message or "")
    if match:
        try:
            tokens = shlex.split(message[match.end():])
        except ValueError as exc:
            raise ValueError("名称引号未配对，请用双引号包住带空格的名称") from exc
    else:
        tokens = [value.strip() for value in (character, work) if value and value.strip()]

    values: dict[str, str] = {}
    positional: list[str] = []
    for token in tokens:
        parts = re.split(r"[=＝:：]", token, maxsplit=1)
        key = _LABELS.get(parts[0].strip().lower()) if len(parts) == 2 else None
        if key is None and "=" not in token and "＝" not in token:
            positional.append(token)
            continue
        if key is None:
            raise ValueError("只支持角色=名称和作品=名称")
        value = parts[1].strip()
        if not value:
            raise ValueError("角色和作品的名称不能留空")
        if key in values:
            raise ValueError("同一字段只能指定一次")
        values[key] = value

    unassigned = [key for key in ("character", "work") if key not in values]
    if len(positional) > len(unassigned):
        raise ValueError("最多指定一个角色和一个作品，带空格的名称请加双引号")
    for key, value in zip(unassigned, positional):
        values[key] = value.strip()
    return values.get("character", ""), values.get("work", "")
