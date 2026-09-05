"""维护服务实现。"""

import asyncio
import os
from typing import TYPE_CHECKING

from astrbot.api import logger

from ..util.safe_io import safe_remove_file
from ..util.retention import count_capacity_managed

if TYPE_CHECKING:
    from ...main import Main  # noqa: F401


def format_capacity_warning(
    total: int, max_reg: int, auto_cleanup: bool, capacity_cmd: str
) -> str | None:
    """拼一条「表情库超出容量上限」的告警。

    启动巡检和每小时巡检共用同一套措辞，免得两处各写一版、说法还不一致。

    Args:
        total: 当前表情包总数。
        max_reg: 配置的容量上限，<= 0 表示不限制。
        auto_cleanup: 是否开启了「容量超限自动清理」。
        capacity_cmd: 手动清理命令（按用户实际唤醒前缀渲染好的）。

    Returns:
        告警文本；没设上限或没超限时返回 None。
    """
    if max_reg <= 0 or total <= max_reg:
        return None
    overflow = total - max_reg
    head = f"[容量控制] 表情包 {total} 张，超出上限 {max_reg} 共 {overflow} 张。"
    if auto_cleanup:
        return (
            head + "「容量超限自动清理」是开启的，后台每小时会永久删除最旧的 "
            f"{overflow} 张（含图片文件，收藏的不删）。"
            "想全部留着请把「最大表情包数量」调大或设为 0（不限制），"
            "也可以直接关掉「容量超限自动清理」"
        )
    return (
        head + "自动清理是关闭的，没有删除任何文件。"
        f"要清理请手动执行 {capacity_cmd}；不想再看到这条提醒，"
        "就把「最大表情包数量」调大或设为 0（不限制）；"
        "想让它每小时自动删最旧的，请打开「容量超限自动清理」"
    )


class MaintenanceService:
    """统一管理插件的维护任务：

    - 启动时：一次性孤儿扫描 + 遗留文件清理
    - 周期任务：raw 目录清理、容量控制
    """

    RAW_CLEANUP_INTERVAL_SECONDS = 30 * 60
    CAPACITY_CONTROL_INTERVAL_SECONDS = 60 * 60

    # 一次孤儿清理最多允许删掉多少比例的文件，超过就只告警不动手。
    # 孤儿判定完全依赖「数据库里有没有这条记录」，只要数据库读得不完整，
    # 这里就会把用户正常的表情包当垃圾删掉，所以必须设上限。
    ORPHAN_DELETE_RATIO_LIMIT = 0.2
    ORPHAN_DELETE_MIN_COUNT = 20

    def __init__(self, plugin: "Main") -> None:
        self.plugin = plugin
        self._tasks: list[asyncio.Task] = []
        # 容量超限告警去重：同一个数量只提醒一次，避免每小时刷日志
        self._capacity_warned_count: int = -1

    @staticmethod
    def _norm_path(value: object) -> str:
        """统一路径写法后再比较。

        大小写、斜杠方向、`.` / `..` 都会让两个指向同一个文件的字符串不相等，
        直接用原始字符串比对会把正常文件误判成孤儿。
        """
        try:
            return os.path.normcase(os.path.abspath(os.path.normpath(str(value))))
        except Exception:
            return str(value)

    async def _remove_orphan_files(
        self, orphans: list[str], total_files: int, scope: str
    ) -> None:
        """删除未登记文件，带比例上限保护。"""
        if not orphans:
            return

        limit = max(
            self.ORPHAN_DELETE_MIN_COUNT,
            int(total_files * self.ORPHAN_DELETE_RATIO_LIMIT),
        )
        if len(orphans) > limit:
            logger.warning(
                f"[Orphan] {scope} 目录里有 {len(orphans)}/{total_files} 个文件不在数据库中，"
                f"超过安全阈值 {limit}，本次不删除任何文件。"
                f"这通常说明索引损坏或数据目录被改动过，建议先重建索引再排查"
            )
            return

        preview = ", ".join(os.path.basename(p) for p in orphans[:10])
        if len(orphans) > 10:
            preview += " …"
        logger.info(
            f"[Orphan] 清理 {scope} 目录 {len(orphans)} 个未登记文件"
            f"（目录共 {total_files} 个）: {preview}"
        )
        for fpath in orphans:
            await safe_remove_file(fpath)

    async def run_startup_cleanup(self) -> None:
        """启动阶段调用：执行一次性清理（遗留文件 + 孤儿扫描）。"""
        await self._clean_legacy_files()
        await self._cleanup_orphans()

    def start_periodic_tasks(self) -> None:
        """注册并启动周期任务。"""
        scheduler = getattr(self.plugin, "task_scheduler", None)
        if scheduler is None:
            logger.warning("[Maintenance] task_scheduler 未初始化，跳过周期任务")
            return

        self._tasks.append(
            scheduler.create_task("raw_cleanup_loop", self._raw_cleanup_loop())
        )
        self._tasks.append(
            scheduler.create_task(
                "capacity_control_loop", self._capacity_control_loop()
            )
        )

    def cancel_all(self) -> None:
        """终止所有周期任务。"""
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()

    async def _raw_cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.RAW_CLEANUP_INTERVAL_SECONDS)
                handler = getattr(self.plugin, "event_handler", None)
                if handler:
                    await handler._clean_raw_directory()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"raw 清理循环出错: {e}")

    async def _capacity_control_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.CAPACITY_CONTROL_INTERVAL_SECONDS)
                await self._check_capacity_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"容量控制循环出错: {e}")

    async def _check_capacity_once(self) -> None:
        """每小时巡检容量。

        默认只告警不删除：后台静默删掉用户攒了几个月的表情包，
        从日志里只能看到一行 INFO，用户根本无从察觉。
        想恢复自动清理请打开配置项 capacity_auto_cleanup。
        """
        cfg = getattr(self.plugin, "plugin_config", None)
        if cfg is None:
            return
        try:
            max_reg = int(getattr(cfg, "max_reg_num", 0) or 0)
        except (TypeError, ValueError):
            return
        if max_reg <= 0:
            return  # 0 = 不限制容量

        idx = await self.plugin.index_manager.load_index()
        # 只数参与容量淘汰的条目：外部源导入的托管副本不占额度，
        # 否则导入一个大包就会天天报"超出上限"却又删不掉任何东西。
        managed = count_capacity_managed(idx)
        overflow = managed - max_reg
        if overflow <= 0:
            self._capacity_warned_count = -1
            return

        if not getattr(cfg, "capacity_auto_cleanup", False):
            if self._capacity_warned_count != managed:
                self._capacity_warned_count = managed
                msg = format_capacity_warning(
                    managed, max_reg, False, self.plugin.cmd("capacity")
                )
                if msg:
                    logger.warning(msg)
            return

        handler = getattr(self.plugin, "event_handler", None)
        if handler:
            await handler._enforce_capacity(idx)
        await self.plugin.index_manager.save_index(idx)

    async def warn_capacity_pressure(self) -> str | None:
        """启动时报一次容量超限，只告警、绝不删除。

        每小时那轮巡检要先 sleep 一小时才跑第一次，重启频繁的机器可能永远
        等不到它；而容量上限是本插件唯一会永久删表情包的开关，出事的典型
        顺序是「从旧版本升级 → 配置文件里还存着老默认值 100 → 删掉最旧的
        几十张 → 几天后才在 WebUI 发现少了」。所以启动时就把话讲清楚。

        Returns:
            实际写进日志的告警文本；没设上限或没超限时返回 None。
        """
        cfg = getattr(self.plugin, "plugin_config", None)
        if cfg is None:
            return None
        try:
            max_reg = int(getattr(cfg, "max_reg_num", 0) or 0)
        except (TypeError, ValueError):
            return None
        if max_reg <= 0:
            return None  # 0 = 不限制容量
        try:
            idx = await self.plugin.index_manager.load_index()
        except Exception as e:
            logger.debug(f"[容量控制] 启动巡检跳过: {e}")
            return None
        managed = count_capacity_managed(idx)
        msg = format_capacity_warning(
            managed,
            max_reg,
            bool(getattr(cfg, "capacity_auto_cleanup", False)),
            self.plugin.cmd("capacity"),
        )
        if msg:
            # 记下来，免得一小时后那轮巡检把同一句话再念一遍
            self._capacity_warned_count = managed
            logger.warning(msg)
        return msg

    async def _cleanup_orphans(self) -> None:
        """清理无文件索引 / 无索引文件。"""
        try:
            db = self.plugin.db_service
            if not db:
                return

            all_paths = db.get_all_paths()

            if all_paths:
                stale_paths = [
                    p for p in all_paths if p and not os.path.isfile(str(p))
                ]
                if stale_paths:
                    # 数据目录没挂载好 / 被整体移动时，这里会看到「全部文件都不见了」。
                    # 旧代码会把整张索引表清空，等目录回来时数据已经没了。
                    stale_limit = max(
                        self.ORPHAN_DELETE_MIN_COUNT,
                        int(len(all_paths) * self.ORPHAN_DELETE_RATIO_LIMIT),
                    )
                    if len(stale_paths) > stale_limit:
                        logger.warning(
                            f"[Orphan] {len(stale_paths)}/{len(all_paths)} 条索引指向的文件都不存在，"
                            f"超过安全阈值 {stale_limit}，本次不改动数据库。"
                            f"请先确认数据目录是否挂载正常或被移动过"
                        )
                        stale_paths = []
                if stale_paths:
                    await db.delete_paths(stale_paths)
                    logger.info(
                        f"[Orphan] 清除 {len(stale_paths)} 条失效索引（文件已丢失）"
                    )

                pending_rows = db.get_pending_paginated(page=1, page_size=100000)
                if pending_rows and pending_rows[0]:
                    stale_ids = [
                        r["id"]
                        for r in pending_rows[0]
                        if r.get("path") and not os.path.isfile(str(r.get("path")))
                    ]
                    if stale_ids:
                        db.delete_pending_batch(stale_ids)
                        logger.info(
                            f"[Orphan] 清除 {len(stale_ids)} 条失效待审核记录"
                        )

                db_count = db.count_total()
                if db_count == 0:
                    return

                db_paths = {self._norm_path(p) for p in all_paths}
                pending_db_paths = {
                    self._norm_path(r.get("path"))
                    for r in (pending_rows[0] if pending_rows else [])
                    if r.get("path")
                }

                categories_dir = str(self.plugin.plugin_config.categories_dir)
                if os.path.isdir(categories_dir):
                    cat_total = 0
                    cat_orphans: list[str] = []
                    for root, _dirs, files in os.walk(categories_dir):
                        for fname in files:
                            cat_total += 1
                            fpath = os.path.join(root, fname)
                            if self._norm_path(fpath) not in db_paths:
                                cat_orphans.append(fpath)
                    await self._remove_orphan_files(cat_orphans, cat_total, "categories")

                pending_dir = str(self.plugin.plugin_config.pending_dir)
                if os.path.isdir(pending_dir):
                    pending_total = 0
                    pending_orphans: list[str] = []
                    for fname in os.listdir(pending_dir):
                        fpath = os.path.join(pending_dir, fname)
                        if not os.path.isfile(fpath):
                            continue
                        pending_total += 1
                        if self._norm_path(fpath) not in pending_db_paths:
                            pending_orphans.append(fpath)
                    await self._remove_orphan_files(
                        pending_orphans, pending_total, "pending"
                    )
        except Exception as e:
            logger.debug(f"[Orphan] 孤儿扫描异常（不阻塞）: {e}")

    async def _clean_legacy_files(self) -> None:
        """删除迁移残留文件：.backup / .migrated / index.json 等。"""
        try:
            db_count = self.plugin.db_service.count_total()
            if db_count <= 0:
                return
            keep_cache_names = {
                "image_cache.json",
                "text_cache.json",
                "bm25_cache.json",
                "desc_cache.json",
                "blacklist_cache.json",
            }
            deleted = 0

            cache_dir = self.plugin.cache_dir
            if cache_dir.is_dir():
                for child in cache_dir.iterdir():
                    name = child.name
                    if name in keep_cache_names:
                        continue
                    if child.is_dir():
                        continue
                    if name.endswith(".wal") or name.endswith(".shm") or name == "emoji.db":
                        continue
                    if (
                        name.endswith(".backup")
                        or name.endswith(".migrated")
                        or name in {"index_cache.json", "index.json"}
                    ):
                        if await safe_remove_file(str(child)):
                            deleted += 1

            categories_dir = self.plugin.plugin_config.categories_dir
            if categories_dir.is_dir():
                for cat_dir in categories_dir.iterdir():
                    if not cat_dir.is_dir():
                        continue
                    legacy_idx = cat_dir / "index.json"
                    if legacy_idx.is_file() and await safe_remove_file(str(legacy_idx)):
                        deleted += 1

            for name in ("index.json", "image_index.json"):
                candidate = self.plugin.base_dir / name
                if candidate.is_file() and await safe_remove_file(str(candidate)):
                    deleted += 1

            if deleted > 0:
                logger.info(f"[清理] 已删除 {deleted} 个遗留文件")
        except Exception as e:
            logger.warning(f"[清理] 遗留文件删除失败: {e}")