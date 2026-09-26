"""Explicit live-Feishu demo execution path.

This module deliberately does not use :class:`factory.engine.Engine`.  Demo
records are allowed to exercise export and feedback bookkeeping, but never
the production generation state machine or the production metrics importer.
"""
from __future__ import annotations

import json
import math
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .media import inspect_image
from .metrics import parse_metrics
from .schema import TABLES
from .state import State
from .util import (
    FactoryError,
    atomic_write,
    file_hash,
    links,
    now,
    safe_id,
    text,
)

DEMO_NATURE = "模拟数据"
_REVIEW_CHOICES = {"通过", "驳回", "撤回"}


def _field_names(table: str) -> list[str]:
    return [field["field_name"] for field in TABLES[table]["fields"]]


def _same_cell(actual: Any, expected: Any) -> bool:
    if isinstance(expected, list) and all(isinstance(x, str) for x in expected):
        try:
            return links(actual) == expected
        except FactoryError:
            return isinstance(actual, list) and all(isinstance(x, str) for x in actual) and actual == expected
    if isinstance(actual, (int, float)) and not isinstance(actual, bool) and isinstance(expected, (int, float)) and not isinstance(expected, bool):
        return math.isclose(float(actual), float(expected), rel_tol=1e-12, abs_tol=1e-15)
    return actual == expected


def _read_one(base, table: str, record_id: str) -> dict:
    rows = [row for row in base.list_records(table, field_names=_field_names(table))
            if row.get("record_id") == record_id]
    if len(rows) != 1:
        raise FactoryError(f"未能通过 typed record-list 唯一读回：{table}/{record_id}")
    return rows[0]


def _find_unique(base, table: str, field: str, value: str) -> dict | None:
    rows = [
        row for row in base.list_records(table, field_names=[field])
        if text(row["fields"].get(field)) == value
    ]
    if len(rows) > 1:
        raise FactoryError(f"{table}.{field} 出现重复 DEMO 业务标识：{value}")
    return _read_one(base, table, rows[0]["record_id"]) if rows else None


def _verify_fields(base, table: str, record_id: str, expected: dict, label: str) -> dict:
    row = _read_one(base, table, record_id)
    for name, value in expected.items():
        if not _same_cell(row["fields"].get(name), value):
            raise FactoryError(f"{label} 读回不一致：{name}")
    return row


def _one_link(value: Any, label: str) -> str:
    values = links(value)
    if len(values) != 1:
        raise FactoryError(f"{label} 必须恰好关联一条记录。")
    return values[0]


def _review_time(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        raw = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError as exc:
            raise FactoryError("DEMO 审核创建时间格式不受支持。") from exc
    raise FactoryError("DEMO 审核缺少系统创建时间，无法判断最新有效结论。")


def _demo_task_base(base, task_id: str) -> tuple[dict, dict, str]:
    task = _read_one(base, "tasks", safe_id(task_id))
    tf = task["fields"]
    if tf.get("示例数据") is not True:
        raise FactoryError("Demo path 只接受 示例数据=true 的任务。")
    if tf.get("取消") is True or tf.get("提交") is not True:
        raise FactoryError("DEMO 任务必须已提交且未取消。")
    product_id = _one_link(tf.get("商品"), "Demo 商品")
    product = _read_one(base, "products", product_id)
    if product["fields"].get("示例数据") is not True:
        raise FactoryError("Demo 任务关联了非 Demo 商品，拒绝混合处理。")
    return task, product, product_id


def _candidate_rows(base, task: dict, product_id: str) -> list[dict]:
    task_id = task["record_id"]
    rows = base.list_records(
        "assets",
        field_names=[
            "资产ID", "素材名", "商品", "来源任务", "槽位", "运行ID", "图片",
            "SHA256", "示例数据", "类型", "审核状态", "资产角色", "模式",
        ],
    )
    candidates = []
    for row in rows:
        fields = row["fields"]
        if links(fields.get("来源任务")) != [task_id]:
            continue
        role=text(fields.get("资产角色"))
        if role and role!="candidate":
            continue
        if fields.get("示例数据") is not True:
            raise FactoryError("Demo 任务关联了非 Demo 输出素材，拒绝导出。")
        if links(fields.get("商品")) != [product_id]:
            raise FactoryError("Demo 候选与任务商品不一致。")
        asset_type = text(fields.get("类型"))
        if asset_type not in {"生成成图", "场景参考"}:
            raise FactoryError("Demo 任务候选素材类型必须是 生成成图或预生成的场景参考。")
        if not text(fields.get("资产ID")) or not text(fields.get("槽位")):
            raise FactoryError("Demo 候选缺少稳定资产 ID 或槽位。")
        attachments = fields.get("图片") or []
        if len(attachments) != 1 or not attachments[0].get("file_token"):
            raise FactoryError(f"Demo 候选 {row['record_id']} 附件数量或 token 不正确。")
        if len(text(fields.get("SHA256"))) != 64:
            raise FactoryError(f"Demo 候选 {row['record_id']} 缺少有效 SHA256。")
        candidates.append(row)
    candidates.sort(key=lambda row: text(row["fields"].get("槽位")))
    expected_count = task["fields"].get("数量")
    if not isinstance(expected_count, (int, float)) or int(expected_count) != expected_count:
        raise FactoryError("DEMO 任务数量不是整数。")
    if len(candidates) != int(expected_count):
        raise FactoryError(f"DEMO 任务候选数量不符：任务={expected_count}，关联输出={len(candidates)}。")
    if len({text(row["fields"].get("槽位")) for row in candidates}) != len(candidates):
        raise FactoryError("DEMO 候选槽位重复。")
    return candidates


def _latest_review(
    base, asset_id: str, *, allowed_record_ids: set[str] | None = None
) -> tuple[str, dict]:
    rows = [
        row for row in base.list_records(
            "reviews",
            field_names=[
                "审核标题", "图片资产", "目标SHA256", "结论", "审核人",
                "商品准确", "品牌及渠道检查", "人工确认", "原因", "创建时间", "创建人",
                "Demo视觉检查", "仅限演示使用", "审核策略版本", "运行ID", "审核版本",
            ],
        )
        if links(row["fields"].get("图片资产")) == [asset_id]
        and (allowed_record_ids is None or row["record_id"] in allowed_record_ids)
    ]
    if not rows:
        raise FactoryError(f"素材 {asset_id} 缺少人工审核记录。")
    ordered = sorted(
        ((_review_time(row["fields"].get("创建时间")), row["record_id"], row) for row in rows),
        key=lambda item: (item[0], item[1]),
    )
    latest_time = ordered[-1][0]
    same_time = [item for item in ordered if item[0] == latest_time]
    choices = {text(item[2]["fields"].get("结论")) for item in same_time}
    if len(choices) > 1:
        raise FactoryError(f"素材 {asset_id} 存在同一时间的冲突审核结论。")
    row = ordered[-1][2]
    fields = row["fields"]
    reviewer = links(fields.get("审核人"))
    creator = links(fields.get("创建人"))
    if len(reviewer) != 1 or len(creator) != 1 or reviewer != creator:
        raise FactoryError(f"审核 {row['record_id']} 缺少一致的审核人/系统创建人。")
    allowed = getattr(base, "config", {}).get("reviewer_open_ids", [])
    if allowed and reviewer[0] not in allowed:
        raise FactoryError(f"审核 {row['record_id']} 的审核人不在允许的审核人列表。")
    choice = text(fields.get("结论"))
    if choice not in _REVIEW_CHOICES:
        raise FactoryError(f"审核 {row['record_id']} 尚未填写有效结论。")
    if fields.get("人工确认") is not True:
        raise FactoryError(f"审核 {row['record_id']} 未勾选人工确认。")
    return choice, row


def _approved_demo_context(
    base, task_id: str, *, allowed_review_record_ids: set[str] | None = None
) -> dict:
    task, product, product_id = _demo_task_base(base, task_id)
    task_fields = task["fields"]
    policy = text(task_fields.get("审核策略版本")).strip() or "legacy-demo-v1"
    namespace = text(task_fields.get("命名空间")).strip()
    if policy == "demo-v2":
        if not namespace.startswith("V1-DEMO-KIDS") or text(task_fields.get("模式")) != "demo":
            raise FactoryError("demo-v2 只接受明确的 V1-DEMO-KIDS / demo 任务。")
        run_id = text(task_fields.get("运行ID"))
        if not run_id:
            raise FactoryError("demo-v2 任务缺少 Runtime 运行ID。")
    elif policy != "legacy-demo-v1":
        raise FactoryError("Demo 审核策略版本不受支持。")
    candidates = _candidate_rows(base, task, product_id)
    decisions = {}
    for row in candidates:
        aid = row["record_id"]
        fields = row["fields"]
        choice, review = _latest_review(
            base, aid, allowed_record_ids=allowed_review_record_ids
        )
        if text(review["fields"].get("目标SHA256")) != text(fields.get("SHA256")):
            raise FactoryError(f"审核 {review['record_id']} 指向了错误的图片版本。")
        review_fields = review["fields"]
        if policy == "demo-v2":
            if text(review_fields.get("审核策略版本")) != policy:
                raise FactoryError(f"审核 {review['record_id']} 的审核策略版本不匹配。")
            if text(review_fields.get("运行ID")) != run_id:
                raise FactoryError(f"审核 {review['record_id']} 未绑定当前 Runtime 运行。")
            version = review_fields.get("审核版本")
            if isinstance(version, bool) or not isinstance(version, (int, float)) or int(version) != version or version < 1:
                raise FactoryError(f"审核 {review['record_id']} 缺少有效审核版本。")
            if choice == "通过" and review_fields.get("Demo视觉检查") is not True:
                raise FactoryError(f"审核 {review['record_id']} 未完成 Demo 视觉检查。")
            if choice == "通过" and review_fields.get("仅限演示使用") is not True:
                raise FactoryError(f"审核 {review['record_id']} 未确认仅限演示使用。")
        else:
            if choice == "通过" and review_fields.get("商品准确") is not True:
                raise FactoryError(f"审核 {review['record_id']} 未确认商品准确。")
            if choice == "通过" and review_fields.get("品牌及渠道检查") is not True:
                raise FactoryError(f"审核 {review['record_id']} 未完成品牌及渠道检查。")
        decisions[aid] = {"decision": choice, "review": review}
    approved = [row for row in candidates if decisions[row["record_id"]]["decision"] == "通过"]
    if not approved:
        raise FactoryError("没有有效通过的 Demo 图片，拒绝生成空交付包。")
    return {
        "task": task,
        "product": product,
        "product_id": product_id,
        "candidates": candidates,
        "decisions": decisions,
        "approved": approved,
        "review_policy_version": policy,
    }


def _download_checked(base, row: dict, destination: Path) -> dict:
    fields = row["fields"]
    attachment = (fields.get("图片") or [])[0]
    destination.parent.mkdir(parents=True, exist_ok=True)
    base.download_attachment("assets", row["record_id"], attachment, destination)
    info = inspect_image(destination)
    registered = text(fields.get("SHA256")).lower()
    if info["sha256"].lower() != registered:
        raise FactoryError(f"Demo 资产 {row['record_id']} 下载后 SHA256 不一致。")
    return {"attachment": attachment, "info": info}


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(entries.items()):
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name:
                raise FactoryError("Demo ZIP 文件名不安全。")
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return buffer.getvalue()


def _remote_attachment(base, table: str, record_id: str, field: str, name: str, size: int):
    row = _read_one(base, table, record_id)
    matches = [a for a in (row["fields"].get(field) or []) if a.get("name") == name and a.get("size") == size]
    if len(matches) > 1:
        raise FactoryError(f"{table}/{record_id} 存在重复附件：{name}")
    return matches[0] if matches else None


def demo_export(
    base,
    state: State,
    task_id: str,
    *,
    allowed_review_record_ids: set[str] | None = None,
) -> dict:
    """Export approved pre-generated Demo assets without creating a production run."""
    context = _approved_demo_context(
        base, task_id, allowed_review_record_ids=allowed_review_record_ids
    )
    task = context["task"]; product = context["product"]
    root = state.root / "demo-deliveries" / safe_id(task_id)
    root.mkdir(parents=True, exist_ok=True)
    entries: dict[str, bytes] = {}
    images = []
    for row in context["approved"]:
        aid = text(row["fields"].get("资产ID"))
        attachment = (row["fields"].get("图片") or [])[0]
        suffix = Path(attachment.get("name", "image.png")).suffix.lower() or ".png"
        local = root / (aid + suffix)
        checked = _download_checked(base, row, local)
        review = context["decisions"][row["record_id"]]["review"]
        images.append({
            "filename": f"images/{aid}{suffix}",
            "asset_id": aid,
            "asset_record_id": row["record_id"],
            "slot": text(row["fields"].get("槽位")),
            "sha256": checked["info"]["sha256"],
            "byte_length": checked["info"]["bytes"],
            "review_record_id": review["record_id"],
            "review_title": text(review["fields"].get("审核标题")),
            "source": "Feishu Demo asset attachment",
        })
        entries[f"images/{aid}{suffix}"] = local.read_bytes()
    manifest = {
        "schema_version": 1,
        "mode": "DEMO",
        "is_demo": True,
        "synthetic": True,
        "pre_generated": True,
        "image_producer_executed": False,
        "not_real_product_facts": True,
        "not_brand_authorization": True,
        "note": "预生成素材导入演示；Workflow 仅为关联配置，未执行真实生图或合成步骤。",
        "task_id": task["record_id"],
        "task_name": text(task["fields"].get("任务名")),
        "sku": text(product["fields"].get("SKU")),
        "product_record_id": context["product_id"],
        "workflow": task["fields"].get("流程"),
        "review_policy_version": context["review_policy_version"],
        "delivered_count": len(images),
        "images": images,
    }
    entries["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    entries["DEMO-README.md"] = (
        "# Image-Factory Demo Delivery\n\n"
        "本包仅用于 Image-Factory 演示。图片是 synthetic、pre-generated Demo 素材，"
        "不是真实 TOPSTAR 商品事实，也不代表品牌授权。\n\n"
        "本次未执行真实 Image Producer；Workflow 仅作为关联配置，未声称生图或合成步骤已执行。\n"
        "禁止发布、投放或用于真实经营判断。\n"
    ).encode()
    content = _zip_bytes(entries)
    zip_sha = file_hash_bytes(content)
    filename = f"{text(task['fields'].get('任务名'))}_{zip_sha[:12]}.zip"
    output = state.root / "demo-deliveries" / filename
    atomic_write(output, content)

    key = f"demo-export:{task['record_id']}:{file_hash(output)}"
    remote = state.effect(
        key,
        lambda: _remote_attachment(base, "tasks", task["record_id"], "已批准交付包", output.name, output.stat().st_size),
        lambda: base.upload_attachment("tasks", task["record_id"], "已批准交付包", output),
    )
    remote = _remote_attachment(base, "tasks", task["record_id"], "已批准交付包", output.name, output.stat().st_size) or remote
    if not remote or not remote.get("file_token"):
        raise FactoryError("Demo 交付 ZIP 上传后未能读回附件。")
    downloaded = state.root / "demo-deliveries" / (output.stem + ".download.zip")
    base.download_attachment("tasks", task["record_id"], remote, downloaded)
    if file_hash(downloaded) != file_hash(output):
        raise FactoryError("Demo 交付 ZIP 下载后 SHA256 不一致。")
    with zipfile.ZipFile(downloaded) as archive:
        names = set(archive.namelist())
        expected_names = {"manifest.json", "DEMO-README.md"} | {item["filename"] for item in images}
        if names != expected_names:
            raise FactoryError("Demo ZIP 内容与批准资产清单不一致。")
        downloaded_manifest = json.loads(archive.read("manifest.json"))
        if downloaded_manifest != manifest:
            raise FactoryError("Demo ZIP manifest 读回不一致。")
        for item in images:
            if file_hash_bytes(archive.read(item["filename"])) != item["sha256"]:
                raise FactoryError("Demo ZIP 内图片 SHA256 校验失败。")
    return {
        "status": "PASS",
        "task_id": task["record_id"],
        "zip": str(output),
        "zip_sha256": file_hash(output),
        "downloaded_zip_sha256": file_hash(downloaded),
        "round_trip_sha256_equal": True,
        "delivered_count": len(images),
        "manifest": manifest,
    }


def file_hash_bytes(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


def create_demo_usage(
    base,
    state: State,
    task_id: str,
    asset_id: str,
    usage_id: str,
    channel: str,
    account: str,
    placement: str,
    page_id: str,
    start_time: str,
) -> dict:
    context = _approved_demo_context(base, task_id)
    safe_id(usage_id)
    row = next((item for item in context["approved"] if item["record_id"] == safe_id(asset_id)), None)
    if row is None:
        raise FactoryError("Demo 使用记录只能关联已通过审核的 Demo 资产。")
    fields = {
        "使用记录名": f"DEMO / {usage_id}",
        "使用ID": usage_id,
        "图片资产": [row["record_id"]],
        "目标SHA256": text(row["fields"].get("SHA256")),
        "渠道": channel,
        "店铺或账户": account,
        "版位": placement,
        "广告或页面ID": page_id,
        "上线时间": start_time,
        "对照说明": "DEMO ONLY；模拟使用，不代表真实发布、投放或上线。",
        "人工确认已上线": False,
        "模式": ["demo"],
    }
    existing = _find_unique(base, "placements", "使用ID", usage_id)
    if existing:
        _verify_fields(base, "placements", existing["record_id"], fields, "Demo 使用记录")
        return {"record_id": existing["record_id"], "status": "existing"}
    record = state.effect(
        f"demo-use:{usage_id}",
        lambda: _find_unique(base, "placements", "使用ID", usage_id),
        lambda: base.create_record("placements", fields),
    )
    rid = record["record_id"]
    _verify_fields(base, "placements", rid, fields, "Demo 使用记录")
    return {"record_id": rid, "status": "created"}


def _demo_usage_context(base, task_id: str, usage_id: str) -> tuple[dict, dict, dict]:
    task, product, product_id = _demo_task_base(base, task_id)
    usage = _find_unique(base, "placements", "使用ID", usage_id)
    if not usage:
        raise FactoryError(f"找不到 Demo 使用记录：{usage_id}")
    usage_fields = usage["fields"]
    asset_id = _one_link(usage_fields.get("图片资产"), "Demo 使用图片")
    asset = _read_one(base, "assets", asset_id)
    af = asset["fields"]
    if af.get("示例数据") is not True or links(af.get("来源任务")) != [task["record_id"]]:
        raise FactoryError("Demo 使用记录未关联当前 Demo 任务的 Demo 资产。")
    if text(usage_fields.get("目标SHA256")) != text(af.get("SHA256")):
        raise FactoryError("Demo 使用记录与图片版本 SHA256 不一致。")
    if usage_fields.get("人工确认已上线") is True:
        raise FactoryError("Demo 使用记录不能被标记为真实上线。")
    choice, review = _latest_review(base, asset_id)
    if choice != "通过" or text(review["fields"].get("目标SHA256")) != text(af.get("SHA256")):
        raise FactoryError("Demo 指标只能关联最新有效通过审核的图片版本。")
    return task, product, usage


def _ensure_attachment(base, state: State, table: str, record_id: str, field: str, path: Path):
    def find():
        return _remote_attachment(base, table, record_id, field, path.name, path.stat().st_size)

    found = state.effect(
        f"demo-evidence:{table}:{record_id}:{field}:{file_hash(path)}",
        find,
        lambda: base.upload_attachment(table, record_id, field, path),
    )
    found = find() or found
    if not found or not found.get("file_token"):
        raise FactoryError("Demo 来源文件上传后未能读回。")
    return found


def demo_import_metrics(base, state: State, task_id: str, csv_path: Path, revision: int = 1) -> dict:
    if csv_path.stat().st_size > 10 * 1024 * 1024:
        raise FactoryError("Demo 指标 CSV 超过 10MB。")
    parsed = parse_metrics(csv_path.read_text(encoding="utf-8-sig"))
    if any(row["source_kind"] != "demo" for row in parsed):
        raise FactoryError("Demo 指标 CSV 的 source_kind 必须全部为 demo。")
    if not isinstance(revision, int) or revision < 1:
        raise FactoryError("Demo 指标版本必须是正整数。")
    raw = csv_path.read_bytes()
    source_sha = file_hash(csv_path)
    evidence = state.root / "demo-imports" / (source_sha + ".csv")
    atomic_write(evidence, raw)
    old_rows = base.list_records("metrics", field_names=_field_names("metrics"))
    result = []
    for row in parsed:
        task, product, usage = _demo_usage_context(base, task_id, row["placement_id"])
        usage_id = usage["fields"]["使用ID"]
        existing = [r for r in old_rows if text(r["fields"].get("逻辑键")) == row["key"]]
        for old in existing:
            old_nature = text(old["fields"].get("数据性质"))
            if old_nature not in {DEMO_NATURE}:
                raise FactoryError("Demo 逻辑键与正式指标冲突，停止导入。")
        existing.sort(key=lambda r: r["fields"].get("版本", 0))
        latest = existing[-1] if existing else None
        fields = {
            "逻辑键": row["key"],
            "版本": revision,
            "使用记录": [usage["record_id"]],
            "日期": row["date"],
            "时区": row["timezone"],
            "渠道": row["channel"],
            "曝光": row["impressions"],
            "点击": row["clicks"],
            "订单": row["orders"],
            "点击率": row["ctr"],
            "每点击订单率": row["orders_per_click"],
            "指标口径": row["metric_definition"],
            "数据性质": DEMO_NATURE,
            "模式": ["demo"],
            "来源文件SHA256": source_sha,
        }
        if latest:
            old_fields = latest["fields"]
            comparable = (
                "使用记录", "日期", "时区", "渠道", "曝光", "点击", "订单",
                "点击率", "每点击订单率", "指标口径", "数据性质", "来源文件SHA256",
            )
            if all(_same_cell(old_fields.get(name), fields.get(name)) for name in comparable):
                _ensure_attachment(base, state, "metrics", latest["record_id"], "来源文件", evidence)
                result.append({"record_id": latest["record_id"], "status": "existing"})
                continue
            raise FactoryError("Demo 同一逻辑键已有不同数据；本版本不自动创建修正版。")
        if revision != 1:
            raise FactoryError("Demo 首次指标导入必须使用版本 1。")
        metric_id = "DEMO-METRIC-" + row["key"][:20] + "-V" + str(revision)
        fields.update({
            "指标ID": metric_id,
            "指标记录名": f"DEMO / {usage_id} / {row['date']} / v{revision}",
            "样本提醒": row["sample_note"],
        })
        record = state.effect(
            f"demo-metric:{metric_id}",
            lambda metric_id=metric_id: _find_unique(base, "metrics", "指标ID", metric_id),
            lambda fields=fields: base.create_record("metrics", fields),
        )
        rid = record["record_id"]
        _verify_fields(base, "metrics", rid, fields, "Demo 指标")
        _ensure_attachment(base, state, "metrics", rid, "来源文件", evidence)
        result.append({"record_id": rid, "status": "created"})
    return {"status": "PASS", "task_id": task_id, "source_sha256": source_sha, "records": result}


def demo_metric_summary(base, task_id: str) -> list[dict]:
    task, product, product_id = _demo_task_base(base, task_id)
    placements = base.list_records("placements", field_names=["使用ID", "图片资产"])
    demo_usage_ids = set()
    for placement in placements:
        asset_ids = links(placement["fields"].get("图片资产"))
        if len(asset_ids) != 1:
            continue
        asset = _read_one(base, "assets", asset_ids[0])
        if asset["fields"].get("示例数据") is True and links(asset["fields"].get("来源任务")) == [task["record_id"]]:
            demo_usage_ids.add(text(placement["fields"].get("使用ID")))
    latest = {}
    seen = set()
    for row in base.list_records("metrics", field_names=_field_names("metrics")):
        fields = row["fields"]
        if text(fields.get("数据性质")) != DEMO_NATURE:
            continue
        usage_record_id = _one_link(fields.get("使用记录"), "Demo 指标使用记录")
        placement = _read_one(base, "placements", usage_record_id)
        actual_usage = text(placement["fields"].get("使用ID"))
        if actual_usage not in demo_usage_ids:
            continue
        key = text(fields.get("逻辑键")); version = fields.get("版本", 0)
        if (key, version) in seen:
            raise FactoryError("Demo 指标出现重复逻辑键/版本。")
        seen.add((key, version))
        if key not in latest or version > latest[key]["fields"].get("版本", 0):
            latest[key] = row
    return list(latest.values())
