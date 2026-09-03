"""命令前缀与命令提示文本的统一渲染入口。

背景：AstrBot 的唤醒前缀 `wake_prefix` 是用户可配置项（顶层配置，类型为 list，
默认 `["/"]`）。有人改成 `!`、`#`、`.`，也有人直接留空，只靠 @机器人 或私聊唤醒。
所以插件里所有"下一步请执行 xxx"这类提示都不能硬编码 `/`，否则会把用户引到一条
在他机器上根本不存在的命令上。

本模块提供三件事：
1. `resolve_wake_prefix()` —— 从 AstrBot 配置里读出当前生效的唤醒前缀；
2. `format_command()` —— 拼出一条可以直接复制粘贴的完整命令；
3. `command_like_pattern()` —— 生成"这段文本看起来像在调用本插件命令"的正则。

注意：Python 函数的 docstring 不参与动态渲染。AstrBot 会把 docstring 当作
`/help` 的说明文案，它在插件加载时就被读取，拿不到会话上下文，因此 docstring
里统一写不带前缀的形式（例如 `用法: mp list [分类]`）。
"""

import re
from typing import Any

# 当前命令组名。短好记，打字快，也是 README / help 里展示的主命令。
COMMAND_GROUP = "mp"

# 1.0.x 使用的旧命令组名，作为别名长期保留，避免老用户的习惯和文档失效。
LEGACY_COMMAND_GROUP = "magpie"

# 中文别名，同样注册为命令组别名。
CHINESE_COMMAND_GROUP = "神偷"

# 读不到配置时的兜底前缀，与 AstrBot 出厂默认值一致。
DEFAULT_WAKE_PREFIX = "/"


def resolve_wake_prefix(context: Any, umo: str | None = None) -> str:
    """返回当前生效的唤醒前缀字符串。

    Args:
        context: 插件的 `Context` 对象（`Star.context`）。
        umo: 会话标识 `unified_msg_origin`。传入时读取该会话的会话级配置，
            不传则读全局配置。

    Returns:
        单个前缀字符串。配置里是列表时取第一项；用户把前缀清空
        （`[]` 或 `[""]`）时返回空字符串，表示"直接写命令名即可"。
        任何异常都回退到 `DEFAULT_WAKE_PREFIX`，保证提示文案永远能渲染出来。
    """
    try:
        config = context.get_config(umo=umo) if umo else context.get_config()
        raw = config.get("wake_prefix", None)
    except Exception:
        return DEFAULT_WAKE_PREFIX

    if raw is None:
        return DEFAULT_WAKE_PREFIX

    if isinstance(raw, str):
        return raw

    if isinstance(raw, (list, tuple)):
        if not raw:
            # 用户主动清空了前缀，此时 AstrBot 只靠 @ / 私聊唤醒，命令本身不带前缀。
            return ""
        for item in raw:
            if isinstance(item, str):
                return item
        return DEFAULT_WAKE_PREFIX

    return DEFAULT_WAKE_PREFIX


def format_command(
    sub: str = "",
    prefix: str = DEFAULT_WAKE_PREFIX,
    group: str = COMMAND_GROUP,
) -> str:
    """拼出一条完整命令，例如 `format_command("migrate apply", "!")` -> `!mp migrate apply`。

    Args:
        sub: 子命令及其参数，例如 `"migrate apply"`。留空则只返回命令组本身。
        prefix: 唤醒前缀，通常来自 `resolve_wake_prefix()`。
        group: 命令组名，默认 `mp`。

    Returns:
        可直接复制执行的命令文本。
    """
    head = f"{prefix or ''}{group}"
    tail = (sub or "").strip()
    return f"{head} {tail}" if tail else head


def command_like_pattern(
    prefix: str = DEFAULT_WAKE_PREFIX,
    groups: tuple[str, ...] = (
        COMMAND_GROUP,
        LEGACY_COMMAND_GROUP,
        CHINESE_COMMAND_GROUP,
    ),
) -> str:
    """生成用于判断"这段文本像不像在调用本插件命令"的正则。

    用于自动发表情的闸门：如果机器人自己的回复里在复述命令用法，就不要再追加表情包。

    Args:
        prefix: 唤醒前缀。会被正则转义，`|` 之类的字符不会破坏表达式。
        groups: 需要匹配的命令组名集合，默认覆盖新名、旧名和中文别名。

    Returns:
        正则字符串，匹配 `<前缀><命令组> <子命令>` 形态的文本。前缀可有可无，
        因为私聊和 @机器人 场景下 AstrBot 会剥掉前缀。
    """
    escaped_prefix = re.escape(prefix) if prefix else ""
    alternatives = "|".join(re.escape(g) for g in groups if g)
    optional_prefix = f"(?:{escaped_prefix})?" if escaped_prefix else ""
    # 前面加一个"不能紧跟 ASCII 字母/数字"的回看，否则 "amp 3" 之类普通聊天
    # 会被误判成在叫命令（中文别名不受影响，"用神偷 发图" 仍能命中）。
    return rf"(?<![0-9A-Za-z_]){optional_prefix}(?:{alternatives})\s+\w+"
