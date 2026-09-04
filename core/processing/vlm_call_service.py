"""VLM 调用服务：负责调用视觉模型分析图片。"""

import asyncio
import os
import tempfile
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

try:
    from PIL import Image as PILImage
    from PIL import ImageDraw as PILImageDraw
except Exception:
    PILImage = None
    PILImageDraw = None

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
        支持指数退避重试。对于 GIF 动图，会提取关键帧拼接后分析。

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
                    "图片从左到右按时间顺序展示同一角色/同一画面的不同时刻；帧之间有分隔线，左上角数字是帧序号。"
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
        """为 VLM 分析准备图片：动图抽帧拼接，上游不收的格式转 PNG。

        这里不看文件扩展名，一律用 Pillow 嗅探真实格式。WebUI 上传、批量导入和
        从旧插件迁移过来的文件经常名实不符（``.gif`` 里装着静态 PNG，``.png``
        里装着 GIF），只按后缀判断会把 ``image/gif`` 这类上游明确拒收的格式直接
        送出去，换回来一个 4xx/5xx。

        Args:
            img_path: 原始图片路径

        Returns:
            tuple[str, bool]: (准备好的图片路径, 是否为动图拼接)
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

        # 动图（GIF / 动态 WebP / APNG 都算）：抽关键帧横向拼接，让 VLM 看懂动作
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
                        f"{actual_frames} 帧, 输出尺寸: {final_w}x{final_h}"
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
        """
        with PILImage.open(fp) as im:
            try:
                im.seek(0)
            except Exception:
                pass
            frame = im.convert("RGBA")

        width, height = frame.size
        longest = max(width, height)
        if longest > MAX_VLM_DIMENSION:
            ratio = MAX_VLM_DIMENSION / longest
            frame = frame.resize(
                (max(1, int(width * ratio)), max(1, int(height * ratio))),
                PILImage.LANCZOS,
            )

        canvas = PILImage.new("RGB", frame.size, (255, 255, 255))
        canvas.paste(frame, (0, 0), frame)

        suffix = ".jpg" if target == "JPEG" else ".png"
        temp_fd, temp_path = tempfile.mkstemp(suffix=suffix)
        os.close(temp_fd)
        if target == "JPEG":
            canvas.save(temp_path, "JPEG", quality=92, optimize=True)
        else:
            canvas.save(temp_path, "PNG", optimize=True)
        return temp_path

    @staticmethod
    def _extract_and_combine_frames(
        fp: str, n_frames: int, width: int, height: int
    ) -> tuple[str, int, int, int]:
        """抽取动图关键帧并横向拼成时间序列长图（阻塞，需放线程池）。

        Returns:
            tuple[str, int, int, int]: (临时图路径, 实际帧数, 输出宽, 输出高)
        """
        MAX_FRAMES = 12  # 最终最多保留 12 帧
        MAX_DECODE_FRAMES = 60  # 最多解码 60 帧，避免长动图吃掉几百 MB 内存
        TARGET_HEIGHT = 480  # 输出高度（提高以保留小字可读性）
        SIMILARITY_THRESHOLD = 1000.0  # 相似帧过滤阈值 (MSE)

        # 只缩不放：原图比目标矮时保持原尺寸，避免画布底部留出大片黑边
        if height > TARGET_HEIGHT:
            scale = TARGET_HEIGHT / height
            frame_width = max(1, int(width * scale))
            frame_height = TARGET_HEIGHT
        else:
            scale = 1.0
            frame_width = max(1, width)
            frame_height = max(1, height)

        with PILImage.open(fp) as im:
            # 先按步长粗采样解码，长动图不必逐帧都留在内存里
            decode_step = max(1, n_frames // MAX_DECODE_FRAMES)
            all_frames = []
            for idx in range(0, n_frames, decode_step):
                im.seek(idx)
                frame = im.convert("RGBA")
                if scale < 1.0:
                    frame = frame.resize((frame_width, frame_height), PILImage.LANCZOS)
                all_frames.append(frame)

        # 相似帧过滤：只留下画面确实变了的帧
        frames = []
        last_selected_np = None
        for frame in all_frames:
            frame_np = np.array(frame, dtype=np.float32)
            if last_selected_np is None:
                frames.append(frame)
                last_selected_np = frame_np
                continue
            if np.mean((frame_np - last_selected_np) ** 2) > SIMILARITY_THRESHOLD:
                frames.append(frame)
                last_selected_np = frame_np

        # 过滤后帧数太少：均匀采样补回来
        if len(frames) < 3 and len(all_frames) >= 3:
            step = max(1, len(all_frames) // 6)
            frames = [all_frames[i] for i in range(0, len(all_frames), step)][:6]

        # 限制最大帧数（均匀抽取）
        if len(frames) > MAX_FRAMES:
            step = len(frames) / MAX_FRAMES
            frames = [frames[int(i * step)] for i in range(MAX_FRAMES)]

        # 横向拼接所有帧，黑色背景代表透明
        total_width = frame_width * len(frames)
        combined = PILImage.new("RGBA", (total_width, frame_height), (0, 0, 0, 255))
        for i, frame in enumerate(frames):
            combined.paste(frame, (i * frame_width, 0), frame)  # 使用帧的 alpha 通道

        # 画帧分隔线和帧序号，帮助 VLM 理解这是时间序列而不是多人并排。
        if PILImageDraw is not None:
            draw = PILImageDraw.Draw(combined)
            for i in range(len(frames)):
                x = i * frame_width
                if i > 0:
                    draw.line([(x, 0), (x, frame_height)], fill=(255, 255, 255, 160), width=2)
                draw.text((x + 4, 4), str(i + 1), fill=(255, 255, 255, 255))

        # 拼接图超出 VLM 输入限制时等比缩放到限制内
        if total_width > MAX_VLM_DIMENSION or frame_height > MAX_VLM_DIMENSION:
            scale_factor = min(
                MAX_VLM_DIMENSION / total_width,
                MAX_VLM_DIMENSION / frame_height,
            )
            new_w = max(1, int(total_width * scale_factor))
            new_h = max(1, int(frame_height * scale_factor))
            combined = combined.resize((new_w, new_h), PILImage.LANCZOS)
            logger.debug(
                f"动图拼接图超出 VLM 限制({total_width}x{frame_height})，已缩放至 {new_w}x{new_h}"
            )

        # 用无损 PNG：JPEG 压缩会把小字压糊，而且 RGBA 也无法直接存 JPEG
        temp_fd, temp_path = tempfile.mkstemp(suffix=".png")
        os.close(temp_fd)
        combined.save(temp_path, "PNG", optimize=True)

        final_w, final_h = combined.size
        return temp_path, len(frames), final_w, final_h

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
