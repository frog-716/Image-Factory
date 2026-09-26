from __future__ import annotations
import warnings
from pathlib import Path
from PIL import Image, ImageOps, UnidentifiedImageError
from .util import FactoryError, atomic_write, file_hash

Image.MAX_IMAGE_PIXELS = 25_000_000

def inspect_image(path: Path, max_bytes=20*1024*1024) -> dict:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= max_bytes:
        raise FactoryError("图片缺失、为空、为软链接或超过 20MB。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                width, height = image.size
                fmt = image.format
                if fmt not in {"PNG","JPEG","WEBP"}:
                    raise FactoryError("仅接受 PNG、JPEG、WEBP 静态图片。")
                if getattr(image,"n_frames",1) != 1:
                    raise FactoryError("不接受动画图片。")
                if min(width,height) < 32 or width*height > 25_000_000:
                    raise FactoryError("图片尺寸不符合安全范围。")
                image.verify()
            with Image.open(path) as image:
                image.load()
                alpha = image.convert("RGBA").getchannel("A").getextrema()
        return {"sha256":file_hash(path),"width":width,"height":height,"format":fmt,
                "has_transparency":alpha[0] < 255,"bytes":path.stat().st_size}
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise FactoryError("图片损坏或触发解码安全限制。") from exc

def compose(background: Path, product: Path, target: Path, size: tuple[int,int], fraction: float) -> dict:
    product_info = inspect_image(product)
    inspect_image(background)
    if not product_info["has_transparency"]:
        raise FactoryError("背景合成模式需要人工确认的透明商品 PNG。白底照片请先抠图并人工核对，禁止暗中重画商品。")
    with Image.open(background) as image:
        canvas = ImageOps.fit(ImageOps.exif_transpose(image).convert("RGBA"), size, method=Image.Resampling.LANCZOS)
    with Image.open(product) as image:
        original = ImageOps.exif_transpose(image).convert("RGBA")
        box = original.getchannel("A").getbbox()
        if box is None:
            raise FactoryError("商品图片完全透明。")
        cutout = original.crop(box)
        scale = min(size[0]*fraction/cutout.width, size[1]*0.72/cutout.height)
        transformed = cutout.resize((max(1,round(cutout.width*scale)),max(1,round(cutout.height*scale))),Image.Resampling.LANCZOS)
        position = ((size[0]-transformed.width)//2, round(size[1]*0.60-transformed.height/2))
        canvas.alpha_composite(transformed, position)
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(target,"PNG")
    return {"operation":"original_cutout_composite_v1","source_sha256":product_info["sha256"],
            "alpha_crop":list(box),"scale":scale,"position":list(position),"canvas":list(size)}
