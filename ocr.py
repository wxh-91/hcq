import asyncio
import io
import threading
from dataclasses import dataclass

import aiohttp


@dataclass(frozen=True)
class OCRResult:
    """Result of one image OCR attempt."""

    text: str = ""
    error: str = ""

    @property
    def succeeded(self) -> bool:
        return bool(self.text)


# PaddleOCR 全局实例（懒加载）
_ocr = None
_ocr_lock = threading.Lock()


def get_ocr():
    """创建 PaddleOCR 实例（懒加载）"""
    global _ocr
    if _ocr is None:
        with _ocr_lock:
            if _ocr is None:
                from paddleocr_onnx import PaddleOCR
                _ocr = PaddleOCR(
                    use_angle_cls=True,
                    lang="ch",
               )
    return _ocr


async def recognize_image_url(image_url: str | None, timeout: int = 15, retries: int = 2) -> OCRResult:
    """下载图片并识别，支持重试（共 retries+1 次尝试）"""
    if not image_url:
        return OCRResult(error="图片链接为空")
    
    last_error = ""
    for attempt in range(retries + 1):
        try:
            client_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.get(image_url) as response:
                    if response.status != 200:
                        last_error = f"HTTP {response.status}"
                        continue  # 重试
                    image_bytes = await response.read()
            
            from PIL import Image, ImageEnhance
            import numpy as np
            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            # 简单增强对比度（鲁棒性）
            enhancer = ImageEnhance.Contrast(image)
            image = enhancer.enhance(1.2)
            image_np = np.array(image)
            
            ocr = get_ocr()
            result = await asyncio.to_thread(ocr.ocr, image_np)
            if not result or not result[0]:
                last_error = "未识别到文字"
                continue  # 重试（可能图片模糊）
            
            lines = []
            for line in result[0]:
                text = line[1][0].strip()
                if text:
                    lines.append(text)
            full_text = "\n".join(lines)
            if not full_text:
                last_error = "文本为空"
                continue
            return OCRResult(text=full_text)
        
        except asyncio.TimeoutError:
            last_error = "下载超时"
            continue
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
    
    # 所有尝试失败
    return OCRResult(error=f"识别失败（多次重试）: {last_error}")
