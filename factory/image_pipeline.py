"""Deterministic V1 kids-shoes image transforms and prompt recipes.

This module is deliberately local-only.  It does not call an image provider, inspect
credentials, or infer transparency by removing white-colored pixels.  The caller is
responsible for supplying a real RGBA product source and independently generated
background files.
"""

from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .util import FactoryError, digest, file_hash


CATEGORY = "kids_shoes"

# These values are part of the V1 processing contract.  Keep them stable: changing
# one changes candidate geometry and therefore the lineage of every later output.
FROZEN_GEOMETRY = {
    "subject_width_fraction": 0.62,
    "subject_height_fraction": 0.38,
    "baseline_y_fraction": 0.83,
    "shadow": "none",
    "resampling": "LANCZOS",
    "alpha_policy": "real-alpha-only",
    "crop_policy": "alpha-bbox-only",
}

# An opt-in processing recipe for future runs.  V1 remains the default so a
# frozen run can never be silently re-composed with different pixels.
GROUNDED_GEOMETRY = {
    **FROZEN_GEOMETRY,
    "subject_width_fraction": 0.75,
    "subject_height_fraction": 0.47,
    "baseline_y_fraction": 0.86,
    "shadow": "alpha-contact-contour",
}

_SCENE_TEXT = {
    "outdoor": "空旷的现代儿童活动区，浅蓝远景和少量柔和绿植；前景为平整浅灰运动地面。",
    "indoor": "奶油白儿童房，浅木色地面；后方仅少量无品牌积木，背景虚化。",
    "studio": "奶油白无缝摄影棚，淡蓝渐变远景；平整浅色承托面，无装饰物。",
}
SCENES = dict(_SCENE_TEXT)

PRODUCT_SOURCE = """独立生成一张无品牌虚构童鞋商品原图：一双完整的白色和浅蓝色儿童运动鞋，
三分之四侧前视角，真实商业产品摄影质感。鞋带、鞋底和鞋口清晰，主体完整不截断。
柔和左上方主光，主体之间保留空隙，四周留边。透明背景 PNG，不画白底，不画棋盘格。
不添加人物、文字、Logo、尺码、价格、认证、功能图标或营销宣称。只输出这一张商品图。"""


class ContractError(FactoryError):
    """The request or input image violates the V1 image contract."""


class Blocked(ContractError):
    """The operation is blocked by a missing permission or unsafe input."""


def _check_category(category: str) -> None:
    if category != CATEGORY:
        raise ContractError("V1 仅支持 category=kids_shoes。")


def _regular_file(path: str | Path, label: str) -> Path:
    value = Path(path)
    if value.is_symlink() or not value.is_file():
        raise ContractError(f"{label} 必须是本地普通文件。")
    return value


def _output_path(path: str | Path, label: str) -> Path:
    value = Path(path)
    if value.exists() and value.is_symlink():
        raise ContractError(f"{label} 不能是软链接。")
    if value.name in {"", ".", ".."}:
        raise ContractError(f"{label} 无效。")
    value.parent.mkdir(parents=True, exist_ok=True)
    return value


def _load(path: str | Path, label: str):
    """Decode one static image with the same basic safety limit as the factory."""
    from PIL import Image, UnidentifiedImageError

    source = _regular_file(path, label)
    try:
        with Image.open(source) as image:
            if getattr(image, "n_frames", 1) != 1:
                raise ContractError(f"{label} 不接受动画图片。")
            width, height = image.size
            if width < 1 or height < 1 or width * height > 32_000_000:
                raise ContractError(f"{label} 尺寸超过处理限制。")
            image.load()
            return image.copy(), {
                "path": str(source),
                "sha256": file_hash(source),
                "bytes": source.stat().st_size,
                "width": width,
                "height": height,
                "format": image.format or "unknown",
                "mode": image.mode,
            }
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError(f"{label} 无法解码。") from exc


def _output_info(path: Path, width: int, height: int, mode: str = "RGB") -> dict[str, Any]:
    return {
        "path": str(path),
        "sha256": file_hash(path),
        "bytes": path.stat().st_size,
        "width": width,
        "height": height,
        "format": "PNG",
        "mode": mode,
    }


def _lineage(category: str, source: dict[str, Any], output: dict[str, Any], *,
             background: dict[str, Any] | None = None,
             processing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a redundant-but-explicit local lineage snapshot.

    ``source``/``background``/``candidate`` make the image relationship easy to
    consume, while ``transforms`` and ``processing`` preserve the exact local
    operation.  The dictionaries are copied so later caller mutation cannot change
    the returned evidence.
    """
    source_copy = copy.deepcopy(source)
    output_copy = copy.deepcopy(output)
    background_copy = copy.deepcopy(background) if background is not None else None
    processing_copy = copy.deepcopy(processing or {})
    return {
        "schema_version": 1,
        "category": category,
        "source": source_copy,
        "product_source": source_copy,
        "raw_output": source_copy,
        "product_white": None,
        "background": background_copy,
        "output": output_copy,
        "candidate": output_copy,
        "processing": processing_copy,
        "transforms": copy.deepcopy(processing_copy.get("transforms", [])),
    }


def validate_product(path: str | Path, category: str = CATEGORY):
    """Validate and load a product source with real alpha transparency.

    A valid source is RGBA and contains both fully transparent and fully opaque
    pixels.  At least two percent of pixels must be transparent and visible, and
    the alpha bounding box must not be degenerate.  No RGB/near-white threshold is
    used, so a white shoe remains part of the visible product.
    """
    _check_category(category)
    image, info = _load(path, "商品源")
    if image.mode != "RGBA":
        raise Blocked("商品源必须是具有真实 alpha 的 RGBA PNG；白底图片不能冒充透明源。")
    alpha = image.getchannel("A")
    alpha_min, alpha_max = alpha.getextrema()
    histogram = alpha.histogram()
    total = image.width * image.height
    transparent_fraction = histogram[0] / total
    visible_fraction = sum(histogram[1:]) / total
    bbox = alpha.getbbox()
    if alpha_min != 0 or alpha_max != 255:
        raise Blocked("商品源必须同时含有 alpha=0 与 alpha=255。")
    if transparent_fraction < 0.02 or visible_fraction < 0.02 or bbox is None:
        raise Blocked("商品源透明区或可见主体不足，拒绝退化输入。")
    if min(bbox[2] - bbox[0], bbox[3] - bbox[1]) < 4:
        raise Blocked("商品源 alpha 边界退化。")
    info.update({
        "role": "product_rgba",
        "alpha_min": alpha_min,
        "alpha_max": alpha_max,
        "transparent_fraction": transparent_fraction,
        "visible_fraction": visible_fraction,
        "bbox": list(bbox),
    })
    return image, info


def derive_white_preview(source: str | Path, target: str | Path,
                         category: str = CATEGORY) -> dict[str, Any]:
    """Derive a human-facing white-background PNG without changing the source."""
    _check_category(category)
    from PIL import Image

    image, source_info = validate_product(source, category=category)
    output_path = _output_path(target, "白底派生图")
    if output_path.resolve() == Path(source).resolve():
        raise ContractError("白底派生图不能覆盖商品透明源。")
    canvas = Image.new("RGBA", image.size, (255, 255, 255, 255))
    canvas.alpha_composite(image)
    canvas.convert("RGB").save(output_path, format="PNG")
    output_info = _output_info(output_path, image.width, image.height)
    processing = {
        "operation": "white_preview_v1",
        "composite": "alpha-over-solid-white",
        "threshold_cutout": False,
        "transforms": [{"operation": "alpha_composite", "background": "#ffffff"}],
    }
    lineage = _lineage(category, source_info, output_info, processing=processing)
    lineage["product_white"] = copy.deepcopy(output_info)
    result = {
        "schema_version": 1,
        "category": category,
        "operation": "white_preview_v1",
        "source": copy.deepcopy(source_info),
        "output": output_info,
        "processing": processing,
        "lineage": lineage,
        # Preserve the useful alpha inspection fields returned by the reference
        # helper, so callers do not need to re-open the source.
        "bbox": source_info["bbox"],
        "transparent_fraction": source_info["transparent_fraction"],
        "visible_fraction": source_info["visible_fraction"],
    }
    return result


def white_preview(source: str | Path, target: str | Path,
                  category: str = CATEGORY) -> dict[str, Any]:
    """Compatibility name matching the reference image helper."""
    return derive_white_preview(source, target, category=category)


def compose(source: str | Path, background: str | Path, target: str | Path,
            category: str = CATEGORY, *, recipe: str = "v1") -> dict[str, Any]:
    """Alpha-composite one product source onto one square background.

    The background is never cropped or tiled.  The only crop recorded in lineage is
    the product's own non-transparent alpha bounding box; this prevents transparent
    padding from changing the frozen placement geometry and is not a collage crop.
    """
    _check_category(category)
    from PIL import Image, ImageDraw, ImageFilter
    if recipe not in {"v1", "grounded-v2"}:
        raise ContractError("不支持的合成版本。")

    product, source_info = validate_product(source, category=category)
    canvas, background_info = _load(background, "背景")
    if canvas.width != canvas.height:
        raise Blocked("背景必须是正方形；不会隐式裁切或拉伸背景。")
    output_path = _output_path(target, "候选图")
    source_resolved = Path(source).resolve()
    background_resolved = Path(background).resolve()
    if output_path.resolve() in {source_resolved, background_resolved}:
        raise ContractError("候选图不能覆盖输入素材。")

    geometry = copy.deepcopy(FROZEN_GEOMETRY if recipe == "v1" else GROUNDED_GEOMETRY)
    alpha_bbox = tuple(source_info["bbox"])
    foreground = product.crop(alpha_bbox)
    scale = min(
        canvas.width * geometry["subject_width_fraction"] / foreground.width,
        canvas.height * geometry["subject_height_fraction"] / foreground.height,
    )
    size = (
        max(1, round(foreground.width * scale)),
        max(1, round(foreground.height * scale)),
    )
    foreground = foreground.resize(size, Image.Resampling.LANCZOS)
    position = (
        (canvas.width - size[0]) // 2,
        round(canvas.height * geometry["baseline_y_fraction"]) - size[1],
    )
    canvas = canvas.convert("RGBA")
    shadow: str | dict[str, Any] = "none"
    transforms = [
        {"operation": "alpha_bbox_crop", "bbox": list(alpha_bbox)},
        {"operation": "resize", "resampling": geometry["resampling"], "scale": scale},
    ]
    if recipe == "grounded-v2":
        # Follow the visible bottom edge at each x instead of placing one dark
        # oval across empty gaps between shoes.  The foreground itself hides
        # the upper half of this soft contact cue.
        alpha = foreground.getchannel("A")
        pixels = alpha.load()
        blur_radius = max(1, round(canvas.width * 0.006))
        shadow_depth = max(2, round(canvas.height * 0.009))
        mask = Image.new("L", canvas.size, 0)
        pen = ImageDraw.Draw(mask)
        for x in range(size[0]):
            bottom = next((y for y in range(size[1] - 1, -1, -1)
                           if pixels[x, y] >= 24), None)
            if bottom is not None:
                y = position[1] + bottom
                pen.line((position[0] + x, y - 2,
                          position[0] + x, y + shadow_depth), fill=90)
        mask = mask.filter(ImageFilter.GaussianBlur(blur_radius))
        shadow_layer = Image.new("RGBA", canvas.size, (24, 31, 43, 0))
        shadow_layer.putalpha(mask)
        canvas = Image.alpha_composite(canvas, shadow_layer)
        shadow = {"kind": "alpha-contact-contour", "color": "#181f2b",
                  "max_alpha": 90, "blur_radius": blur_radius,
                  "depth": shadow_depth, "alpha_cutoff": 24}
        transforms.append({"operation": "contact_shadow", **shadow})
    canvas.alpha_composite(foreground, position)
    canvas.convert("RGB").save(output_path, format="PNG")
    output_info = _output_info(output_path, canvas.width, canvas.height)
    background_info.update({"role": "background"})
    operation = "alpha-composite-v1" if recipe == "v1" else "alpha-composite-grounded-v2"
    transforms.append({"operation": "alpha_composite", "position": list(position)})
    processing = {
        "operation": operation,
        "geometry": geometry,
        "alpha_bbox": list(alpha_bbox),
        "product_size": list(size),
        "position": list(position),
        "scale": scale,
        "shadow": shadow,
        "transforms": transforms,
        "collage": False,
        "background_crop": False,
    }
    lineage = _lineage(category, source_info, output_info,
                       background=background_info, processing=processing)
    result = {
        "schema_version": 1,
        "category": category,
        "operation": operation,
        "source": copy.deepcopy(source_info),
        "background": copy.deepcopy(background_info),
        "output": output_info,
        "output_size": [canvas.width, canvas.height],
        "product_size": list(size),
        "position": list(position),
        "algorithm": operation,
        "shadow": shadow,
        "warning": "透视、光照和主体接触自然度仍需人工视觉审核。",
        "processing": processing,
        "lineage": lineage,
    }
    return result


def composite(source: str | Path, background: str | Path, target: str | Path,
              category: str = CATEGORY) -> dict[str, Any]:
    """Compatibility name matching the reference image helper."""
    return compose(source, background, target, category=category)


def compose_candidates(source: str | Path, backgrounds: Mapping[str, str | Path],
                       output_dir: str | Path, category: str = CATEGORY, *,
                       recipe: str = "v1") -> dict[str, Any]:
    """Compose the three independent V1 scene candidates from one source.

    This helper only accepts the fixed scene set and writes one PNG per scene.  It
    does not stitch, split, or crop a multi-panel image.
    """
    _check_category(category)
    expected = tuple(_SCENE_TEXT)
    if set(backgrounds) != set(expected):
        raise ContractError("V1 必须分别提供 outdoor、indoor、studio 三张背景。")
    values = list(backgrounds.values())
    resolved = [Path(value).resolve() for value in values]
    if len(set(resolved)) != len(resolved):
        raise ContractError("三个场景必须使用独立背景文件。")
    folder = Path(output_dir)
    folder.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    for scene in expected:
        target = folder / f"candidate-{scene}.png"
        results[scene] = compose(source, backgrounds[scene], target,
                                 category=category, recipe=recipe)
        results[scene]["scene"] = scene
    return results


def _validate_references(references: Sequence[Mapping[str, Any]] | None) -> list[dict[str, str]]:
    """Validate explicit image/description-reference permissions.

    Omitted references mean no references.  In particular, a missing permission is
    never interpreted as true, and an auto-captioned description cannot become a
    bypass for a forbidden source image.
    """
    if references is None:
        return []
    if not isinstance(references, (list, tuple)):
        raise ContractError("references 必须是列表。")
    attachments: list[dict[str, str]] = []
    for reference in references:
        if not isinstance(reference, Mapping):
            raise ContractError("每个 reference 必须是对象。")
        use = reference.get("use")
        if use == "image_reference":
            if reference.get("allow_image_generation") is not True:
                raise Blocked("Image reference lacks explicit generation permission")
            asset_id = reference.get("asset_id")
            sha256 = reference.get("sha256")
            if not isinstance(asset_id, str) or not asset_id.strip():
                raise ContractError("image_reference 缺少 asset_id。")
            if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
                raise ContractError("image_reference 必须提供不可变 sha256。")
            attachments.append({"asset_id": asset_id, "sha256": sha256})
        elif use == "description_reference":
            if (reference.get("description_source") != "human_authored"
                    or reference.get("allow_description_reuse") is not True):
                raise Blocked("Do not bypass restrictions by auto-captioning forbidden images")
        else:
            raise ContractError("Reference role must be explicit")
    return attachments


def background(scene: str, refs: Sequence[Mapping[str, Any]] | None = None,
               category: str = CATEGORY) -> dict[str, Any]:
    """Compile one independent square background recipe for a fixed scene."""
    _check_category(category)
    if scene not in _SCENE_TEXT:
        raise ContractError("Unknown scenario")
    attachments = _validate_references(refs)
    text = (
        "独立生成一张正方形童鞋广告背景，只画背景。" + _SCENE_TEXT[scene]
        + "机位低，柔和左上方主光，偏右后方轻柔投影。中心偏下约六成宽度留作商品摆放区，"
        + "该区域无障碍物。明亮干净、低饱和、真实商业摄影。禁止鞋子、人物、文字、Logo、"
        + "尺寸、认证、价格、促销符号和拼图。不得在地面预画鞋形阴影。"
    )
    return {
        "schema_version": 1,
        "category": category,
        "scene": scene,
        "visual_prompt": text,
        "prompt_hash": digest(text),
        "prompt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "image_references": attachments,
        "output_role": "background",
        "aspect_ratio": "1:1",
        "independent_image_count": 1,
        "reference_policy": "explicit-permission-only",
    }


def product_source_prompt(refs: Sequence[Mapping[str, Any]] | None = None,
                          category: str = CATEGORY) -> str:
    """Return the fixed product-source recipe text after checking references."""
    _check_category(category)
    _validate_references(refs)
    return PRODUCT_SOURCE


def product_source(refs: Sequence[Mapping[str, Any]] | None = None,
                   category: str = CATEGORY) -> dict[str, Any]:
    """Return the product-source recipe using the reference module's naming style."""
    return product_source_recipe(refs=refs, category=category)


def product_prompt(refs: Sequence[Mapping[str, Any]] | None = None,
                   category: str = CATEGORY) -> str:
    """Alias for callers that use the shorter prompt name."""
    return product_source_prompt(refs=refs, category=category)


def product_source_recipe(refs: Sequence[Mapping[str, Any]] | None = None,
                          category: str = CATEGORY) -> dict[str, Any]:
    """Return the product recipe with the same governance shape as backgrounds."""
    text = product_source_prompt(refs=refs, category=category)
    attachments = _validate_references(refs)
    return {
        "schema_version": 1,
        "category": category,
        "visual_prompt": text,
        "prompt_hash": digest(text),
        "prompt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "image_references": attachments,
        "output_role": "product_rgba",
        "independent_image_count": 1,
        "reference_policy": "explicit-permission-only",
    }


class ImagePipeline:
    """Small dependency-free facade for the V1 local operations."""

    category = CATEGORY

    def validate_product(self, path: str | Path):
        return validate_product(path, category=self.category)

    def derive_white_preview(self, source: str | Path, target: str | Path):
        return derive_white_preview(source, target, category=self.category)

    def compose(self, source: str | Path, background: str | Path, target: str | Path):
        return compose(source, background, target, category=self.category)

    def background(self, scene: str, refs: Sequence[Mapping[str, Any]] | None = None):
        return background(scene, refs=refs, category=self.category)


__all__ = [
    "CATEGORY",
    "SCENES",
    "FROZEN_GEOMETRY",
    "PRODUCT_SOURCE",
    "ContractError",
    "Blocked",
    "ImagePipeline",
    "validate_product",
    "derive_white_preview",
    "white_preview",
    "compose",
    "composite",
    "compose_candidates",
    "background",
    "product_source_prompt",
    "product_source",
    "product_prompt",
    "product_source_recipe",
]
