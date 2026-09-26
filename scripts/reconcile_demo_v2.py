#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

from factory.cli import configuration
from factory.lark import FeishuGateway
from factory.schema import TABLES
from factory.state import State
from factory.util import FactoryError, canonical, file_hash, links, safe_id, text, write_json


SKU = "DEMO-TOPSTAR-KIDS-001"
TASK_NAME = "DEMO-RUN-002"
WORKFLOW_NAME = "[IF] 背景合成 v1.0"
SLOT_FILES = {"A": "02-playground.png", "B": "03-bedroom.png", "C": "06-ecommerce.png"}


def args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--state", default="var/live")
    parser.add_argument("--zip", default="TOPSTAR-Kids-Demo-Individual.zip")
    return parser.parse_args()


def field_names(table: str) -> list[str]:
    return [field["field_name"] for field in TABLES[table]["fields"]]


def read_one(base, table: str, record_id: str) -> dict:
    rows = [row for row in base.list_records(table, field_names=field_names(table)) if row["record_id"] == record_id]
    if len(rows) != 1:
        raise FactoryError(f"无法唯一读回 {table}/{record_id}。")
    return rows[0]


def find_unique(base, table: str, field: str, value: str) -> dict | None:
    rows = [row for row in base.list_records(table, field_names=[field]) if text(row["fields"].get(field)) == value]
    if len(rows) > 1:
        raise FactoryError(f"{table}.{field} 出现重复业务标识：{value}。")
    return read_one(base, table, rows[0]["record_id"]) if rows else None


def same(actual, expected) -> bool:
    if isinstance(expected, list) and expected and all(isinstance(item, str) for item in expected):
        try:
            return links(actual) == expected
        except FactoryError:
            return actual == expected
    if expected == []:
        return not actual or actual == []
    return actual == expected


def verify(base, table: str, record_id: str, expected: dict, label: str) -> dict:
    row = read_one(base, table, record_id)
    for name, wanted in expected.items():
        if not same(row["fields"].get(name), wanted):
            raise FactoryError(f"{label} {record_id} 字段 {name} 读回不一致。")
    return row


def load_manifest(archive: Path, source_root: Path):
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        assets = []
        source_root.mkdir(parents=True, exist_ok=True)
        for item in manifest["assets"]:
            data = bundle.read(item["file"])
            actual = hashlib.sha256(data).hexdigest()
            if actual != item["sha256"]:
                raise FactoryError(f"manifest SHA256 不一致：{item['file']}")
            path = source_root / item["file"]
            path.write_bytes(data)
            assets.append({"file": item["file"], "sha256": actual, "bytes": len(data), "path": path})
    if len(assets) != 7:
        raise FactoryError("Demo manifest 不是 7 张素材。")
    return assets


def asset_id_for(index: int, filename: str) -> str:
    stem = Path(filename).stem.upper().replace(" ", "-")
    prefix = f"{index:02d}-"
    if stem.startswith(prefix):
        stem = stem[len(prefix):]
    return f"DEMO-KIDS-ASSET-{index:02d}-{stem}"


def attachment(base, record_id: str, name: str, size: int):
    row = read_one(base, "assets", record_id)
    matches = [item for item in (row["fields"].get("图片") or [])
               if item.get("name") == name and item.get("size") == size]
    if len(matches) > 1:
        raise FactoryError(f"Asset {record_id} 存在重复附件 {name}。")
    return matches[0] if matches else None


def download_verify(base, row: dict, source: dict, destination: Path) -> dict:
    remote = attachment(base, row["record_id"], source["file"], source["bytes"])
    if remote is None:
        raise FactoryError(f"Asset {row['record_id']} 缺少附件 {source['file']}。")
    base.download_attachment("assets", row["record_id"], remote, destination)
    size = destination.stat().st_size
    sha = file_hash(destination)
    if size != source["bytes"] or sha != source["sha256"]:
        raise FactoryError(f"Asset {row['record_id']} 附件 SHA256 不一致。")
    return {"name": source["file"], "bytes": size, "sha256": sha, "file_token": remote.get("file_token")}


def main():
    cli_args = args()
    root = Path.cwd()
    config = configuration(root / cli_args.config)
    base = FeishuGateway(config)
    state = State(root / cli_args.state)
    state.bind_base(base.token)
    checkpoint_path = root / cli_args.state / "demo-v2-reconcile-checkpoint.json"
    checkpoint = {"stage": "start", "assets": [], "task": None, "candidates": [], "reviews": []}
    try:
        product = find_unique(base, "products", "SKU", SKU)
        if not product or product["fields"].get("示例数据") is not True:
            raise FactoryError("Demo SKU 不存在或未标记 示例数据=true。")
        product_id = product["record_id"]
        workflow = find_unique(base, "workflows", "流程名", WORKFLOW_NAME)
        if not workflow:
            raise FactoryError("正式 Workflow 不存在。")
        workflow_id = workflow["record_id"]
        steps = [row for row in base.list_records("steps", field_names=["流程","顺序","启用"])
                 if links(row["fields"].get("流程")) == [workflow_id]]
        if len(steps) != 6:
            raise FactoryError(f"Workflow Steps 不是 6 条：{len(steps)}。")

        sources = load_manifest(root / cli_args.zip, root / cli_args.state / "demo-v2-sources")
        assets = []
        seen_ids = set()
        seen_hashes = set()
        for index, source in enumerate(sources, start=1):
            asset_id = asset_id_for(index, source["file"])
            if asset_id in seen_ids or source["sha256"] in seen_hashes:
                raise FactoryError("Demo Asset 稳定 ID 或 SHA256 重复。")
            seen_ids.add(asset_id); seen_hashes.add(source["sha256"])
            expected = {
                "素材名": f"DEMO / Kids / {source['file']}",
                "资产ID": asset_id,
                "类型": ["场景参考"],
                "商品": [product_id],
                "来源说明": f"DEMO synthetic pre-generated asset; ZIP manifest file={source['file']}; 未执行 Image Producer；非真实商品事实。",
                "授权说明": "DEMO ONLY；不代表真实 TOPSTAR 授权素材；禁止真实发布、投放或生产使用。",
                "允许用于生图": False,
                "SHA256": source["sha256"],
                "示例数据": True,
            }
            row = find_unique(base, "assets", "资产ID", asset_id)
            if row is None:
                row = state.effect(
                    f"demo-v2-reconcile-asset-create:{base.token}:{asset_id}:{source['sha256']}",
                    lambda asset_id=asset_id: find_unique(base, "assets", "资产ID", asset_id),
                    lambda expected=expected: base.create_record("assets", expected),
                )
                row = verify(base, "assets", row["record_id"], expected, "Demo Asset")
            else:
                verify(base, "assets", row["record_id"], expected, "Demo Asset")
            upload_key = f"demo-v2-reconcile-asset-upload:{base.token}:{asset_id}:{source['sha256']}"
            remote = state.effect(
                upload_key,
                lambda row=row, source=source: attachment(base, row["record_id"], source["file"], source["bytes"]),
                lambda row=row, source=source: base.upload_attachment("assets", row["record_id"], "图片", source["path"]),
            )
            row = verify(base, "assets", row["record_id"], expected, "Demo Asset")
            checked = download_verify(base, row, source, root / cli_args.state / "demo-v2-downloads" / source["file"])
            assets.append({"record_id": row["record_id"], "asset_id": asset_id, **checked})
            checkpoint["stage"] = f"asset:{source['file']}"
            checkpoint["assets"] = assets
            write_json(checkpoint_path, checkpoint)

        if len(assets) != 7 or len({item["record_id"] for item in assets}) != 7:
            raise FactoryError("Demo Asset 未达到 7 条唯一记录。")
        if len({item["sha256"] for item in assets}) != 7:
            raise FactoryError("Demo Asset SHA256 出现重复。")

        task_fields = {
            "任务名": TASK_NAME,
            "商品": [product_id],
            "流程": [workflow_id],
            "选用素材": [[item["record_id"] for item in assets]][0],
            "渠道": "DEMO / 不连接真实平台",
            "图片用途": "演示数据",
            "消费场景": "预生成素材管理流程演示",
            "版位": "Demo / 未发布",
            "视觉风格": "预生成 Demo 素材",
            "附加要求": "DEMO；非真实商品事实；不代表 TOPSTAR 授权；禁止真实发布、投放或生产使用。",
            "变体方向": "A=playground；B=bedroom；C=ecommerce；仅使用预生成图片。",
            "数量": 3,
            "最多调用次数": 0,
            "提交": True,
            "取消": False,
            "示例数据": True,
            "系统状态": ["待审核"],
            "错误摘要": "DEMO：使用预生成图片验证管理流程，Image Producer 未实际执行。",
        }
        task = find_unique(base, "tasks", "任务名", TASK_NAME)
        if task is None:
            task = state.effect(
                f"demo-v2-task-create:{base.token}:{TASK_NAME}",
                lambda: find_unique(base, "tasks", "任务名", TASK_NAME),
                lambda: base.create_record("tasks", task_fields),
            )
        task = verify(base, "tasks", task["record_id"], task_fields, "DEMO-RUN-002")
        task_id = task["record_id"]
        checkpoint["stage"] = "task"
        checkpoint["task"] = task_id
        write_json(checkpoint_path, checkpoint)

        assets_by_file = {item["name"]: item for item in assets}
        for slot, filename in SLOT_FILES.items():
            item = assets_by_file[filename]
            row = read_one(base, "assets", item["record_id"])
            current_source = links(row["fields"].get("来源任务"))
            current_slot = text(row["fields"].get("槽位"))
            current_run = text(row["fields"].get("运行ID"))
            current_review = links(row["fields"].get("审核状态"))
            if current_source not in ([], [task_id]) or current_slot not in ("", slot) or current_run not in ("", TASK_NAME) or current_review not in ([], ["待审核"]):
                raise FactoryError(f"Candidate {slot} 已存在冲突字段，停止覆盖。")
            desired = {"来源任务": [task_id], "槽位": slot, "运行ID": TASK_NAME, "审核状态": ["待审核"]}
            if current_source != [task_id] or current_slot != slot or current_run != TASK_NAME or current_review != ["待审核"]:
                state.effect(
                    f"demo-v2-candidate-link:{base.token}:{task_id}:{item['asset_id']}:{slot}",
                    lambda row=row, desired=desired: row if all(same(row["fields"].get(k), v) for k, v in desired.items()) else None,
                    lambda row=row, desired=desired: base.update_record("assets", row["record_id"], desired),
                )
            verify(base, "assets", item["record_id"], {**{
                "资产ID": item["asset_id"], "SHA256": item["sha256"], "来源任务": [task_id],
            }, **desired}, f"Candidate {slot}")
            checkpoint["stage"] = f"candidate:{slot}"
            checkpoint["candidates"].append({"slot": slot, **item})
            write_json(checkpoint_path, checkpoint)

        reviews = []
        for slot, filename in SLOT_FILES.items():
            item = assets_by_file[filename]
            review_title = f"DEMO-RUN-002 / {slot} / {filename}"
            expected = {
                "审核标题": review_title,
                "图片资产": [item["record_id"]],
                "目标SHA256": item["sha256"],
                "结论": [],
                "审核人": [],
                "商品准确": False,
                "品牌及渠道检查": False,
                "人工确认": False,
                "原因": "待人工审核；DEMO 数据；不得自动审批。",
            }
            existing = find_unique(base, "reviews", "审核标题", review_title)
            if existing is None:
                existing = state.effect(
                    f"demo-v2-review-create:{base.token}:{task_id}:{slot}:{item['asset_id']}:{item['sha256']}",
                    lambda: find_unique(base, "reviews", "审核标题", review_title),
                    lambda expected=expected: base.create_record("reviews", expected),
                )
            review = verify(base, "reviews", existing["record_id"], expected, f"Review {slot}")
            reviews.append({"slot": slot, "record_id": review["record_id"], "initial": expected})
            checkpoint["stage"] = f"review:{slot}"
            checkpoint["reviews"] = reviews
            write_json(checkpoint_path, checkpoint)

        checkpoint["stage"] = "human-review"
        write_json(checkpoint_path, checkpoint)
        print(json.dumps({"status":"PASS","assets":assets,"task_record_id":task_id,"candidates":checkpoint["candidates"],"reviews":reviews,"checkpoint":str(checkpoint_path)},ensure_ascii=False))
    finally:
        state.close()


if __name__ == "__main__":
    main()
