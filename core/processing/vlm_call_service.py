"""VLM 调用服务：负责调用视觉模型分析图片。"""

import asyncio
import math
import os
import tempfile
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

try:
    from PIL import Image as PILImage
    from PIL import ImageDraw as PILImageDraw
    from PIL import ImageFont as PILImageFont
except Exception:
    PILImage = None
    PILImageDraw = None
    PILImageFont = None

try:
    import numpy as np
except Exception:
    np = None


# 主流视觉模型（含各类中转站）稳定接受的图片格式。GIF / BMP / TIFF 这些即使本地
# 能打开，也常被上游直接拒收（例如 Gemini 会报 mime type is not supported），
# 所以送出去之前一律转成白名单里的格式。
VLM_SAFE_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})

# 白名单格式对应的合法扩展名。上游多半按扩展名猜 mime，因此「.gif 里装着 PNG」
# 这种名实不符的文件同样会被拒，必须重写一份。
_FORMAT_SUFFIXES: dict[str, tuple[str, ...]] = {
    "PNG": (".png",),
    "JPEG": (".jpg", ".jpeg"),
    "WEBP": (".webp",),
}

# VLM 输入的最大边长，超过就等比缩小。
MAX_VLM_DIMENSION = 2048

# 动图最终最多拼进去多少帧。再多只能压缩每帧尺寸，画面里的字反而看不清了。
MAX_MONTAGE_FRAMES = 12
# 挑帧阶段最多扫多少个采样点。长动图按步长跳着扫，不逐帧解码。
MAX_SCAN_FRAMES = 60
# 挑帧只比对这么大的缩略指纹：一帧 32x32 的 float32 才 12KB，扫 60 帧也不到 1MB。
FRAME_SIGNATURE_SIZE = 32
# 相邻采样点的指纹 MSE 超过这个值，才算「画面确实变了」，值得单独占一格。
FRAME_DIFF_THRESHOLD = 1000.0
# 拼接图的总像素预算（约 1414x1414）。主流视觉模型内部都会先把图缩到这个量级再
# 切块，继续堆分辨率只会白白拉长编码和上传时间、多烧 token，对识别没有帮助。
MAX_MONTAGE_PIXELS = 2_000_000

# PNG 压缩档位。optimize=True 会把好几种滤波器都试一遍，实测一张两百万像素的图
# 要多花 600~800ms，换来的体积只小一成左右；小机器上这点体积不值得。
PNG_COMPRESS_LEVEL = 6
# 拼接图的体积预算。照片、动漫类素材拼出来的无损 PNG 动辄 3~4MB，base64 之后还要
# 再涨三分之一：上传慢，也容易撞上中转服务的请求体上限。超预算就改存 JPEG。
MONTAGE_MAX_BYTES = 1_500_000
# 超预算时的 JPEG 质量。网格版每帧的像素数是旧版一字排开的好几倍，q90 的压缩痕迹
# 已经不影响读画面里的字了。
MONTAGE_JPEG_QUALITY = 90

# 一张图（或动图的一帧）解码后的像素数超过这个值，就改成按横条带缩放：一次只
# 摊开一条真彩色像素，峰值内存跟图片尺寸脱钩。壁纸级的 4000x4000 一次性摊开就是
# 48MB 起步，再叠上重采样缓冲和并发，小内存机器很容易被打爆。
STRIP_PIXEL_THRESHOLD = 2 * MAX_VLM_DIMENSION * MAX_VLM_DIMENSION
# 每个条带允许摊开多少源像素，约 200 万（真彩色 6MB 上下）。
STRIP_SOURCE_PIXELS = 2_000_000

# 调色板类模式存的是「颜色索引」而不是颜色本身，直接缩放会被 Pillow 降级成
# NEAREST、甚至按索引插值算出完全错误的颜色，必须先展开成真彩色再缩放。
_INDEXED_MODES = frozenset({"P", "PA", "1"})
# 自带 alpha 通道的模式，送出前要合到白底上。
_ALPHA_MODES = frozenset({"RGBA", "LA", "La", "RGBa", "PA"})

# 「这张图我永远不收」的上游特征串：重试多少次都是同样结果，只能换格式重来。
_MIME_REJECT_MARKERS: tuple[str, ...] = (
    "mime type is not supported",
    "mime_type is not supported",
    "unsupported mime",
    "convert_request_failed",
    "unsupported image",
    "invalid image format",
    "不支持的图片格式",
    "不支持的图片类型",
)


class VLMCallService:
    """负责调用视觉模型进行图片分析。"""

    def __init__(self, plugin_instance) -> None:
        self.plugin = plugin_instance
        self.plugin_config = getattr(plugin_instance, "plugin_config", None)
        self.vision_provider_id = (
            str(getattr(self.plugin_config, "vision_provider_id", "") or "")
            if self.plugin_config
            else ""
        )
        self._cached_framework_vlm_id: str | None = None

    async def _resolve_vision_provider(self, event=None) -> str | None:
        """统一的视觉模型 provider 解析逻辑。

        优先级：
        1. 插件配置的 vision_provider_id
        2. AstrBot 框架配置的 default_image_caption_provider_id
        """
        if self.vision_provider_id:
            return self.vision_provider_id

        if self._cached_framework_vlm_id is not None:
            return self._cached_framework_vlm_id or None

        framework_vlm_id = ""
        try:
            if hasattr(self.plugin, "context"):
                astrbot_config = self.plugin.context.get_config()
                provider_settings = astrbot_config.get("provider_settings", {})
                framework_vlm_id = str(
                    provider_settings.get("default_image_caption_provider_id", "") or ""
                )
        except Exception as e:
            logger.debug(f"读取框架视觉模型配置失败: {e}")

        self._cached_framework_vlm_id = framework_vlm_id

        if framework_vlm_id:
            logger.info(f"使用框架全局图片描述模型: {framework_vlm_id}")
            return framework_vlm_id

        logger.warning(
            "未配置视觉模型，无法进行图片分类。"
            "请在插件配置中设置 vision_provider_id，"
            "或在 AstrBot 全局配置中设置 default_image_caption_provider_id。"
        )
        return None

    async def _call_vision_model(
        self, event: AstrMessageEvent | None, img_path: str, prompt: str
    ) -> str:
        """调用视觉模型分析图片。

        使用 context.llm_generate 调用指定的视觉模型 provider，
        支持指数退避重试。对于 GIF 动图，会抽关键帧拼成网格图后分析。

        Args:
            event: 消息事件（用于 provider 解析）
            img_path: 图片绝对路径（调用方需保证已验证）
            prompt: 提示词

        Returns:
            str: 模型响应文本

        Raises:
            ValueError: 未配置视觉模型
            FileNotFoundError: 图片文件不存在
            Exception: 模型调用失败（已重试）
        """
        # 路径规范化
        img_path_obj = Path(img_path)
        if not img_path_obj.is_absolute():
            data_dir = getattr(self.plugin_config, "data_dir", None)
            img_path_obj = (
                (Path(data_dir) / img_path).resolve() if data_dir else img_path_obj.resolve()
            )
        img_path = str(img_path_obj)

        if not os.path.exists(img_path):
            raise FileNotFoundError(f"图片文件不存在: {img_path}")

        # 解析 provider
        provider_id = await self._resolve_vision_provider(event)
        if not provider_id:
            raise ValueError(
                "未配置视觉模型(vision_provider_id)，无法进行图片分析。"
                "请在插件配置或 AstrBot 全局配置中设置。"
            )

        # 预处理：动图抽帧拼接、上游不收的格式转 PNG（可能产生多个临时文件）
        temp_files: list[str] = []
        try:
            actual_img_path, is_animated = await self._prepare_image_for_vlm(img_path)
            if actual_img_path != img_path:
                temp_files.append(actual_img_path)  # 标记为临时文件，分析后删除

            # 直接传入本地绝对路径，框架内部会自动处理路径转换
            resolved_img_path = str(Path(actual_img_path).resolve())

            # 如果是动图拼接，添加专用提示词前缀
            actual_prompt = prompt
            if is_animated:
                animated_prefix = (
                    "[动图帧序列] 这不是多人并排的静态场景，而是一个动态表情包的多帧连续截图。"
                    "这些帧按网格排列：从左到右、从上到下就是时间顺序，展示同一角色/同一画面的不同时刻。"
                    "格子之间有白色分隔线，每格左上角的数字是帧序号。"
                    "黑色背景代表透明区域。"
                    "请以动图/动画的角度理解：这个表情包在表达什么连续动作、表情或情绪变化？"
                    "不要描述成“并排站立”“多人同时出现”或“几个人站在一起”。"
                    "如果画面中有文字（字幕、弹幕、对话框、贴纸文字），请逐字识别并理解语义。\n\n"
                )
                actual_prompt = animated_prefix + prompt

            return await self._do_vlm_call(
                provider_id, actual_prompt, resolved_img_path, temp_files=temp_files
            )
        finally:
            # 清理临时文件（预处理产出 + 格式自救产出）
            for temp_file in temp_files:
                if not temp_file or not os.path.exists(temp_file):
                    continue
                try:
                    os.remove(temp_file)
                    logger.debug(f"已清理临时图片: {temp_file}")
                except Exception as e:
                    logger.warning(f"清理临时文件失败: {e}")

    async def _prepare_image_for_vlm(self, img_path: str) -> tuple[str, bool]:
        """为 VLM 分析准备图片：动图抽帧拼网格，上游不收的格式转 PNG。

        这里不看文件扩展名，一律用 Pillow 嗅探真实格式。WebUI 上传、批量导入和
        从旧插件迁移过来的文件经常名实不符（``.gif`` 里装着静态 PNG，``.png``
        里装着 GIF），只按后缀判断会把 ``image/gif`` 这类上游明确拒收的格式直接
        送出去，换回来一个 4xx/5xx。

        Args:
            img_path: 原始图片路径

        Returns:
            tuple[str, bool]: (准备好的图片路径, 是否为动图拼接图)
        """
        if PILImage is None:
            logger.warning(
                "未安装 Pillow，图片将原样送给视觉模型；"
                "若上游报「mime type is not supported」请安装 Pillow>=10.0.0"
            )
            return img_path, False

        try:
            fmt, is_animated, n_frames, width, height = await asyncio.to_thread(
                self._probe_image, img_path
            )
        except Exception as e:
            logger.warning(f"识别图片真实格式失败，原样送给视觉模型: {e}")
            return img_path, False

        # 动图（GIF / 动态 WebP / APNG 都算）：抽关键帧拼成网格，让 VLM 看懂动作
        if is_animated and n_frames > 1:
            if np is None:
                logger.debug("未安装 numpy，跳过动图抽帧，改为只送首帧")
            else:
                try:
                    temp_path, actual_frames, final_w, final_h = await asyncio.to_thread(
                        self._extract_and_combine_frames, img_path, n_frames, width, height
                    )
                    logger.debug(
                        f"动图拼接完成({fmt or '未知格式'}): {n_frames} 帧 -> "
                        f"{actual_frames} 帧, 输出 {final_w}x{final_h}, "
                        f"{os.path.getsize(temp_path) // 1024}KB"
                    )
                    return temp_path, True
                except Exception as e:
                    logger.warning(f"动图帧提取失败，改为只送首帧: {e}")
            # 抽帧不可用或失败时退化成单帧 PNG，而不是把原始动图直接丢给上游
            return await self._flatten_for_vlm(img_path, "PNG", fmt), False

        # 静态图：真实格式和扩展名都在白名单里才原样放行
        if fmt in VLM_SAFE_FORMATS:
            suffix = Path(img_path).suffix.lower()
            if suffix in _FORMAT_SUFFIXES.get(fmt, ()):
                return img_path, False
            logger.debug(f"扩展名 {suffix or '(无)'} 与真实格式 {fmt} 不一致，重写为 PNG 再送出")
            return await self._flatten_for_vlm(img_path, "PNG", fmt), False

        # GIF / BMP / TIFF / ICO 等上游不收的静态格式，统一转 PNG
        logger.debug(f"静态 {fmt or '未知'} 格式不在视觉模型白名单内，转 PNG 后再送出")
        return await self._flatten_for_vlm(img_path, "PNG", fmt), False

    @staticmethod
    def _probe_image(fp: str) -> tuple[str, bool, int, int, int]:
        """嗅探图片真实信息：(格式, 是否动图, 帧数, 宽, 高)。阻塞，需放线程池。"""
        with PILImage.open(fp) as im:
            fmt = str(getattr(im, "format", "") or "").upper()
            is_animated = bool(getattr(im, "is_animated", False))
            try:
                n_frames = int(getattr(im, "n_frames", 1) or 1)
            except Exception:
                n_frames = 1
            width, height = im.size
        return fmt, is_animated, n_frames, width, height

    async def _flatten_for_vlm(self, img_path: str, target: str, fmt: str = "") -> str:
        """把图片压成单帧、上游一定认的格式；失败时退回原路径。"""
        try:
            return await asyncio.to_thread(self._render_flat_copy, img_path, target)
        except Exception as e:
            logger.warning(f"图片转 {target} 失败（原格式 {fmt or '未知'}），原样送出: {e}")
            return img_path

    @staticmethod
    def _render_flat_copy(fp: str, target: str = "PNG") -> str:
        """把首帧合成到白底后另存为临时文件（阻塞，需放线程池）。

        动图拼接图用黑底（提示词里已声明「黑色背景代表透明区域」），单帧这里改用
        白底：静态表情包多是深色线稿或带白描边的文字，黑底容易把内容糊成一片。

        内存占用只跟像素数有关、跟文件体积无关：一张 27KB 的 4000x4000 纯色 PNG
        展开后同样是 48MB 起步。所以这里尽量先缩小再转模式，并且只有真的带 alpha
        才额外开一张白底画布。
        """
        with PILImage.open(fp) as im:
            try:
                im.seek(0)
            except Exception:
                pass
            # JPEG 能在解码阶段直接按 1/2、1/4、1/8 出图，省掉整张全尺寸位图
            try:
                im.draft("RGB", (MAX_VLM_DIMENSION, MAX_VLM_DIMENSION))
            except Exception:
                pass

            has_alpha = im.mode in _ALPHA_MODES or "transparency" in im.info
            want_mode = "RGBA" if has_alpha else "RGB"
            width, height = im.size
            longest = max(width, height)
            target_size = None
            if longest > MAX_VLM_DIMENSION:
                ratio = MAX_VLM_DIMENSION / longest
                target_size = (max(1, int(width * ratio)), max(1, int(height * ratio)))

            try:
                frame = VLMCallService._detach_frame(im, want_mode, target_size)
            except Exception as e:
                # 冷门模式（I;16、CMYK 变体等）可能不支持先缩放，老实走一遍常规路径
                logger.debug(f"低内存缩放路径不可用，回退常规转换: {e}")
                frame = im.convert(want_mode)

        # 到这里源图句柄已经关掉，全尺寸解码缓冲先还给系统，再做重采样
        if target_size is not None and frame.size != target_size:
            resized = VLMCallService._resize_image(frame, target_size)
            frame.close()
            frame = resized

        if has_alpha:
            canvas = PILImage.new("RGB", frame.size, (255, 255, 255))
            canvas.paste(frame, (0, 0), frame)
            frame.close()
            frame = canvas

        suffix = ".jpg" if target == "JPEG" else ".png"
        temp_fd, temp_path = tempfile.mkstemp(suffix=suffix)
        os.close(temp_fd)
        try:
            if target == "JPEG":
                frame.save(temp_path, "JPEG", quality=92)
            else:
                frame.save(temp_path, "PNG", compress_level=PNG_COMPRESS_LEVEL)
        finally:
            frame.close()
        return temp_path

    @staticmethod
    def _detach_frame(im, want_mode: str, target_size: tuple[int, int] | None):
        """从打开的图片里取出一张独立位图，尽量不留全尺寸中间产物。

        真彩色图先缩放再转模式：反过来先 convert 会凭空多出一整张原始尺寸的位图。
        调色板图必须先展开成真彩色，缩放留给调用方在源图句柄关闭之后再做，免得
        「索引缓冲 + 真彩色缓冲 + 重采样缓冲」三张大图同时挤在内存里。

        返回的一定是新对象，可以安全地在 ``im`` 关闭之后继续使用。
        """
        if target_size is not None and im.width * im.height > STRIP_PIXEL_THRESHOLD:
            return VLMCallService._resize_in_strips(im, want_mode, target_size)
        if im.mode in _INDEXED_MODES:
            return im.convert(want_mode)

        stage = im
        if target_size is not None and stage.size != target_size:
            stage = VLMCallService._resize_image(stage, target_size)
        if stage.mode != want_mode:
            converted = stage.convert(want_mode)
            if stage is not im:
                stage.close()
            return converted
        return im.copy() if stage is im else stage

    @staticmethod
    def _resize_image(im, size: tuple[int, int]):
        """等比缩放。reducing_gap 让 Pillow 先做整数级降采样，少开一张大中间图。"""
        try:
            return im.resize(size, PILImage.LANCZOS, reducing_gap=2.0)
        except TypeError:  # Pillow < 7 没有 reducing_gap
            return im.resize(size, PILImage.LANCZOS)

    @staticmethod
    def _resize_in_strips(im, want_mode: str, target_size: tuple[int, int]):
        """按横条带把超大图缩到目标尺寸：一次只摊开一条真彩色像素。

        条带之间在源图侧留出重叠行，再用 ``resize`` 的 ``box`` 精确取回中间那段，
        这样重采样滤波器在条带边界也有足够邻域，接缝处不会出现色带。
        """
        src_w, src_h = im.size
        tgt_w, tgt_h = target_size
        scale = src_h / tgt_h
        # 每条带摊开的源像素数控制在 STRIP_SOURCE_PIXELS 上下
        budget_rows = int(STRIP_SOURCE_PIXELS * tgt_h / max(1, src_w * src_h))
        rows_per_strip = max(1, min(tgt_h, budget_rows))
        pad = min(src_h, int(math.ceil(3 * scale)) + 1)  # LANCZOS 的支撑半径是 3

        out = PILImage.new(want_mode, target_size)
        for top in range(0, tgt_h, rows_per_strip):
            bottom = min(tgt_h, top + rows_per_strip)
            src_top = min(float(src_h), top * scale)
            src_bottom = min(float(src_h), bottom * scale)
            crop_top = max(0, int(src_top) - pad)
            crop_bottom = min(src_h, int(math.ceil(src_bottom)) + pad)
            piece = im.crop((0, crop_top, src_w, crop_bottom))
            if piece.mode != want_mode:
                converted = piece.convert(want_mode)
                piece.close()
                piece = converted
            resized = piece.resize(
                (tgt_w, bottom - top),
                PILImage.LANCZOS,
                box=(0, src_top - crop_top, src_w, src_bottom - crop_top),
            )
            piece.close()
            out.paste(resized, (0, top))
            resized.close()
        return out

    @staticmethod
    def _label_font(frame_height: int):
        """给帧序号挑一个跟帧尺寸相称的字号，取不到就退回 Pillow 默认位图字体。"""
        if PILImageFont is None:
            return None
        size = max(14, min(48, frame_height // 12))
        try:
            return PILImageFont.load_default(size=size)
        except TypeError:
            pass  # Pillow < 10.1 的 load_default() 不接受 size
        except Exception:
            return None
        try:
            return PILImageFont.load_default()
        except Exception:
            return None

    @staticmethod
    def _draw_montage_guides(
        canvas, cols: int, rows: int, count: int, frame_width: int, frame_height: int
    ) -> None:
        """画格线和帧序号，帮 VLM 认出这是时间序列而不是多人并排。"""
        if PILImageDraw is None:
            return
        draw = PILImageDraw.Draw(canvas)
        width, height = canvas.size
        grid_color = (255, 255, 255, 160)
        for col in range(1, cols):
            x = col * frame_width
            draw.line([(x, 0), (x, height)], fill=grid_color, width=2)
        for row in range(1, rows):
            y = row * frame_height
            draw.line([(0, y), (width, y)], fill=grid_color, width=2)

        font = VLMCallService._label_font(frame_height)
        stroke = 1 if frame_height < 160 else 2
        for slot in range(count):
            xy = ((slot % cols) * frame_width + 4, (slot // cols) * frame_height + 2)
            label = str(slot + 1)
            try:
                draw.text(
                    xy,
                    label,
                    fill=(255, 255, 255, 255),
                    font=font,
                    stroke_width=stroke,
                    stroke_fill=(0, 0, 0, 255),
                )
            except TypeError:  # 老版本 Pillow 的 text() 不认识 stroke_*
                draw.text(xy, label, fill=(255, 255, 255, 255), font=font)

    @staticmethod
    def _extract_and_combine_frames(
        fp: str, n_frames: int, width: int, height: int
    ) -> tuple[str, int, int, int]:
        """抽取动图关键帧，拼成一张接近正方形的网格图（阻塞，需放线程池）。

        分两趟解码，是为了让内存峰值跟源动图的体量脱钩：第一趟每个采样点只留一张
        32x32 的指纹用来挑帧，第二趟才按需解码挑中的那十几帧，贴完立刻释放。整张
        1920x480、120 帧的动图跑下来，占用和一张普通表情包动图是同一个量级。

        排版也从「一字排开」改成网格。12 帧横排时总宽必然远超 2048，压回限制内每
        帧只剩几十像素高，画面里的字全糊了；换成网格后同样的帧数能保住几十倍的像
        素，对识别准确率是净收益。整张图另有 MAX_MONTAGE_PIXELS 的面积预算兜着，
        免得编码和上传的开销跟着分辨率一起失控。

        Returns:
            tuple[str, int, int, int]: (临时图路径, 实际帧数, 输出宽, 输出高)
        """
        # ── 第一趟：只解缩略指纹，挑出画面确实变了的采样点 ──
        scan_step = max(1, n_frames // MAX_SCAN_FRAMES)
        thumb_size = (FRAME_SIGNATURE_SIZE, FRAME_SIGNATURE_SIZE)
        scanned: list[int] = []
        signatures: list = []
        with PILImage.open(fp) as im:
            for idx in range(0, n_frames, scan_step):
                try:
                    im.seek(idx)
                except EOFError:
                    break  # 有些动图头里写的帧数比实际能解出来的多
                thumb = im.convert("RGB").resize(thumb_size, PILImage.BILINEAR)
                signatures.append(np.asarray(thumb, dtype=np.float32))
                thumb.close()
                scanned.append(idx)

        picked: list[int] = []
        last_sig = None
        for idx, sig in zip(scanned, signatures):
            if last_sig is not None:
                if float(np.mean((sig - last_sig) ** 2)) <= FRAME_DIFF_THRESHOLD:
                    continue
            picked.append(idx)
            last_sig = sig
        signatures.clear()

        # 差异帧太少（几乎静止的动图）：均匀采样补回来，至少让 VLM 看到画面全貌
        if len(picked) < 3 and len(scanned) >= 3:
            picked = scanned[:: max(1, len(scanned) // 6)][:6]
        # 帧数上限（均匀抽取）
        if len(picked) > MAX_MONTAGE_FRAMES:
            step = len(picked) / MAX_MONTAGE_FRAMES
            picked = [picked[int(i * step)] for i in range(MAX_MONTAGE_FRAMES)]
        if not picked:
            raise ValueError("这张动图没有解出任何可用的帧")

        # ── 排版：列数取到让整张图接近正方形，每帧才能分到最多的像素 ──
        count = len(picked)
        cols = max(1, min(count, round(math.sqrt(count * height / width)) or 1))
        rows = math.ceil(count / cols)
        # 只缩不放：小图放大只会更糊，还白白多占内存
        scale = min(
            (MAX_VLM_DIMENSION // cols) / width,
            (MAX_VLM_DIMENSION // rows) / height,
            # 长宽各自不超限，乘起来仍可能是四百万像素，再套一层总面积预算
            math.sqrt(MAX_MONTAGE_PIXELS / (cols * rows * width * height)),
            1.0,
        )
        frame_width = max(1, int(width * scale))
        frame_height = max(1, int(height * scale))
        montage_w = frame_width * cols
        montage_h = frame_height * rows
        need_resize = (frame_width, frame_height) != (width, height)
        # 巨幅动图的单帧同样可能有几十 MB，缩放走条带
        strip_frames = need_resize and width * height > STRIP_PIXEL_THRESHOLD

        # ── 第二趟：只解码挑中的帧，贴完就释放。黑底代表透明区域 ──
        combined = PILImage.new("RGBA", (montage_w, montage_h), (0, 0, 0, 255))
        try:
            with PILImage.open(fp) as im:
                for slot, idx in enumerate(picked):
                    im.seek(idx)
                    if strip_frames:
                        frame = VLMCallService._resize_in_strips(
                            im, "RGBA", (frame_width, frame_height)
                        )
                    else:
                        frame = im.convert("RGBA")
                        if need_resize:
                            resized = VLMCallService._resize_image(
                                frame, (frame_width, frame_height)
                            )
                            frame.close()
                            frame = resized
                    combined.paste(
                        frame,
                        ((slot % cols) * frame_width, (slot // cols) * frame_height),
                        frame,  # 用帧自己的 alpha 通道做蒙版
                    )
                    frame.close()

            VLMCallService._draw_montage_guides(
                combined, cols, rows, count, frame_width, frame_height
            )

            # 优先无损 PNG：JPEG 会把小字压糊，而且 RGBA 也没法直接存 JPEG
            temp_fd, temp_path = tempfile.mkstemp(suffix=".png")
            os.close(temp_fd)
            combined.save(temp_path, "PNG", compress_level=PNG_COMPRESS_LEVEL)
            if os.path.getsize(temp_path) > MONTAGE_MAX_BYTES:
                temp_path = VLMCallService._reencode_as_jpeg(temp_path, combined)
        finally:
            combined.close()

        return temp_path, count, montage_w, montage_h

    @staticmethod
    def _reencode_as_jpeg(png_path: str, combined) -> str:
        """把超出体积预算的拼接图改存 JPEG，失败就照旧用 PNG。"""
        jpeg_fd, jpeg_path = tempfile.mkstemp(suffix=".jpg")
        os.close(jpeg_fd)
        try:
            # 半透明的格线丢掉 alpha 之后会变成纯白，正好是想要的效果
            flat = combined.convert("RGB")
            try:
                flat.save(jpeg_path, "JPEG", quality=MONTAGE_JPEG_QUALITY)
            finally:
                flat.close()
        except Exception as e:
            logger.debug(f"拼接图转 JPEG 失败，继续用 PNG: {e}")
            VLMCallService._silent_remove(jpeg_path)
            return png_path
        logger.debug(
            f"拼接图 {os.path.getsize(png_path) // 1024}KB 超出体积预算，"
            f"改存 JPEG {os.path.getsize(jpeg_path) // 1024}KB"
        )
        VLMCallService._silent_remove(png_path)
        return jpeg_path

    @staticmethod
    def _silent_remove(path: str) -> None:
        """删掉用不上的中间文件，删不掉也不影响主流程。"""
        try:
            os.remove(path)
        except OSError:
            pass

    async def _do_vlm_call(
        self,
        provider_id: str,
        prompt: str,
        file_url: str,
        temp_files: list[str] | None = None,
    ) -> str:
        """执行 VLM 调用（带重试 + 格式自救）。

        Args:
            provider_id: 提供商 ID
            prompt: 提示词
            file_url: 图片本地路径或 URL
            temp_files: 调用方的临时文件清单；格式自救产生的新文件会登记进来待清理

        Returns:
            str: 模型响应文本
        """
        # 重试配置
        try:
            max_retries = int(getattr(self.plugin, "vision_max_retries", 3))
        except (TypeError, ValueError):
            max_retries = 3
        try:
            retry_delay = float(getattr(self.plugin, "vision_retry_delay", 1.0))
        except (TypeError, ValueError):
            retry_delay = 1.0
        last_error: Exception | None = None
        converted_once = False
        attempt = 0

        while attempt < max_retries:
            try:
                logger.debug(
                    f"调用VLM (尝试 {attempt + 1}/{max_retries}), "
                    f"provider={provider_id}, 图片={file_url}"
                )
                result = await self._llm_generate_with_image_compat(
                    provider_id=provider_id,
                    prompt=prompt,
                    file_url=file_url,
                )

                # LLMResponse.completion_text 是 @property，自动处理 result_chain
                text = (result.completion_text or "").strip() if result else ""
                if text:
                    logger.debug(f"VLM响应: {text[:200]}")
                    return text

                logger.warning("VLM返回空响应")
                last_error = Exception("VLM返回空响应")

            except Exception as e:
                last_error = e
                error_msg = str(e)

                # 上游明确表示「这个图片格式我不收」：本地转码一次再试。
                # 这类错误重试多少次都是同样结果，而这里的内部重试不经过
                # analysis_throttle 的 RPM 令牌桶，空转会把真实请求速率放大到
                # max_retries 倍，反而更容易把同批的其他图片打成 429。
                if self._is_mime_rejected(e):
                    if converted_once:
                        logger.error(f"本地转码后上游仍拒收该图片，停止重试: {e}")
                        raise
                    converted = await self._force_convert_for_vlm(file_url)
                    if not converted:
                        logger.error(f"上游拒收该图片格式且本地转码失败，停止重试: {e}")
                        raise
                    file_url = converted
                    converted_once = True
                    if temp_files is not None:
                        temp_files.append(converted)
                    logger.warning(
                        "上游不接受该图片格式，已本地转码为 "
                        f"{Path(converted).suffix.lstrip('.').upper()} 后重试: {e}"
                    )
                    continue  # 自救不消耗重试预算，也不需要退避

                is_rate_limit = any(
                    kw in error_msg
                    for kw in (
                        "429",
                        "RateLimit",
                        "exceeded your current request limit",
                    )
                )
                is_provider_error = "Provider" in error_msg or "提供商" in error_msg
                if is_rate_limit:
                    logger.warning(f"VLM请求被限流 ({attempt + 1}/{max_retries})")
                elif is_provider_error:
                    logger.error(
                        f"VLM模型提供商错误 ({attempt + 1}/{max_retries}): {e}\n"
                        f"  当前provider_id: {provider_id}\n"
                        f"  提示: 请检查插件配置中的'视觉模型'是否有效，"
                        f"  或尝试清空该配置使用框架全局的图片描述模型"
                    )
                else:
                    logger.error(f"VLM调用失败 ({attempt + 1}/{max_retries}): {e}")

            attempt += 1
            # 指数退避
            if attempt < max_retries:
                await asyncio.sleep(retry_delay * (2 ** (attempt - 1)))

        raise Exception(f"视觉模型调用失败（已重试{max_retries}次）: {last_error}") from last_error

    @staticmethod
    def _is_mime_rejected(exc: BaseException) -> bool:
        """判断异常是否为「上游不接受这个图片格式」。"""
        text = f"{type(exc).__name__} {exc}".lower()
        return any(marker in text for marker in _MIME_REJECT_MARKERS)

    async def _force_convert_for_vlm(self, file_url: str) -> str | None:
        """上游拒收当前格式时，本地强制转码一份再试；转不了返回 None。

        已经是 PNG 就转 JPEG（少数中转只认 jpeg），否则统一转 PNG。
        只对本地文件生效，传进来的是远端 URL 时直接返回 None。
        """
        if PILImage is None:
            logger.warning("未安装 Pillow，无法本地转码图片格式")
            return None
        try:
            path = Path(file_url)
            if not path.is_file():
                return None
        except OSError:
            return None

        target = "JPEG" if path.suffix.lower() == ".png" else "PNG"
        converted = await self._flatten_for_vlm(str(path), target)
        if not converted or converted == str(path):
            return None  # 转码失败时 _flatten_for_vlm 会退回原路径
        return converted

    async def _llm_generate_with_image_compat(self, provider_id: str, prompt: str, file_url: str):
        """兼容不同 AstrBot 版本对 image_urls 参数形态的处理差异。"""
        try:
            # 优先使用列表形态，避免部分版本把字符串按字符拆分成“多张图片”。
            return await self.plugin.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                image_urls=[file_url],
            )
        except Exception as e:
            # 仅在参数形态/类型不兼容时回退，避免对无关错误重复请求。
            err = str(e).lower()
            fallback_markers = (
                "list object",
                "startswith",
                "image_urls",
                "expected list",
                "expected str",
                "typeerror",
            )
            if not any(marker in err for marker in fallback_markers):
                raise

            logger.warning(f"VLM image_urls 参数形态不兼容，回退为字符串模式重试一次: {e}")
            return await self.plugin.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                image_urls=file_url,
            )
