#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

from factory.cli import configuration
from factory.lark import FeishuGateway
from factory.state import State
from factory.util import FactoryError, file_hash, links, text


SKU = "DEMO-TOPSTAR-KIDS-001"
TASK_NAME = "DEMO-RUN-002"
OLD_FIRST = "recvvLHGgsDebc"
ERRONEOUS_DUPLICATE = "recvvOECiEBMQ1"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.local.json")
    p.add_argument("--state", default="var/live")
    p.add_argument("--zip", default="TOPSTAR-Kids-Demo-Individual.zip")
    return p.parse_args()


def read_one(base, table, record_id, fields):
    rows = [r for r in base.list_records(table, field_names=fields) if r["record_id"] == record_id]
    if len(rows) != 1:
        raise FactoryError(f"无法唯一读回 {table}/{record_id}")
    return rows[0]


def find_unique(base, table, field, value, fields):
    rows = [r for r in base.list_records(table, field_names=[field]) if text(r["fields"].get(field)) == value]
    if len(rows) > 1:
        raise FactoryError(f"{table}.{field} 重复：{value}")
    return read_one(base, table, rows[0]["record_id"], fields) if rows else None


def sha_sources(archive):
    with zipfile.ZipFile(archive) as z:
        manifest = json.loads(z.read("manifest.json"))
        return {item["file"]: (z.read(item["file"],), item["sha256"]) for item in manifest["assets"]}


def main():
    args = parse_args(); root = Path.cwd()
    base = FeishuGateway(configuration(root / args.config))
    state = State(root / args.state); state.bind_base(base.token)
    sources = sha_sources(root / args.zip)
    try:
        first_name = "01-indoor.png"; first_data, first_sha = sources[first_name]
        first_path = root / args.state / "demo-v2-sources" / first_name
        first_path.parent.mkdir(parents=True, exist_ok=True); first_path.write_bytes(first_data)
        first = read_one(base, "assets", OLD_FIRST, ["资产ID","SHA256","商品","图片","示例数据"])
        if first["fields"].get("资产ID") != "DEMO-KIDS-ASSET-01-INDOOR" or first["fields"].get("SHA256") != first_sha:
            raise FactoryError("旧第一条 Asset 的稳定 ID 或 SHA256 不一致，停止。")
        existing = (first["fields"].get("图片") or [])
        if not existing:
            state.effect(
                f"demo-v2-repair-first-upload:{base.token}:{OLD_FIRST}:{first_sha}",
                lambda: None,
                lambda: base.upload_attachment("assets", OLD_FIRST, "图片", first_path),
            )
        first = read_one(base, "assets", OLD_FIRST, ["资产ID","SHA256","商品","图片","示例数据"])
        attachments = first["fields"].get("图片") or []
        if len(attachments) != 1:
            raise FactoryError("旧第一条 Asset 附件数量不是 1。")
        downloaded = root / args.state / "demo-v2-downloads" / first_name
        downloaded.parent.mkdir(parents=True, exist_ok=True)
        base.download_attachment("assets", OLD_FIRST, attachments[0], downloaded)
        if downloaded.stat().st_size != len(first_data) or file_hash(downloaded) != first_sha:
            raise FactoryError("旧第一条 Asset 下载 SHA256 不一致。")

        asset_fields = ["资产ID","SHA256","商品","图片","来源任务","槽位","运行ID","审核状态","示例数据"]
        corrected = {}
        for index, name in enumerate(sorted(sources), start=1):
            if index == 1:
                corrected[name] = OLD_FIRST
                continue
            old_id = f"DEMO-KIDS-ASSET-{index:02d}-{Path(name).stem.upper()}"
            new_id = f"DEMO-KIDS-ASSET-{index:02d}-{Path(name).stem.upper().split('-',1)[1]}"
            row = find_unique(base, "assets", "资产ID", old_id, asset_fields)
            if not row:
                raise FactoryError(f"找不到待校正 Asset：{old_id}")
            conflict = find_unique(base, "assets", "资产ID", new_id, asset_fields)
            if conflict and conflict["record_id"] != row["record_id"]:
                raise FactoryError(f"目标 Asset ID 已被其他记录占用：{new_id}")
            if old_id != new_id:
                state.effect(
                    f"demo-v2-repair-asset-id:{base.token}:{row['record_id']}:{new_id}",
                    lambda row=row, new_id=new_id: row if row["fields"].get("资产ID") == new_id else None,
                    lambda row=row, new_id=new_id: base.update_record("assets", row["record_id"], {"资产ID": new_id}),
                )
                row = read_one(base, "assets", row["record_id"], asset_fields)
                if row["fields"].get("资产ID") != new_id:
                    raise FactoryError(f"Asset ID 校正读回失败：{new_id}")
            corrected[name] = row["record_id"]

        task = find_unique(base, "tasks", "任务名", TASK_NAME, ["任务名","选用素材","示例数据"])
        if not task:
            raise FactoryError("找不到 DEMO-RUN-002。")
        desired_ids = [corrected[name] for name in sorted(corrected)]
        current_ids = links(task["fields"].get("选用素材"))
        if ERRONEOUS_DUPLICATE in current_ids:
            desired_cell = [{"id": rid} for rid in desired_ids]
            state.effect(
                f"demo-v2-repair-task-assets:{base.token}:{task['record_id']}",
                lambda: task if links(task["fields"].get("选用素材")) == desired_ids else None,
                lambda: base.update_record("tasks", task["record_id"], {"选用素材": desired_cell}),
            )
            task = read_one(base, "tasks", task["record_id"], ["任务名","选用素材","示例数据"])
        if links(task["fields"].get("选用素材")) != desired_ids:
            raise FactoryError("DEMO-RUN-002 素材关系校正后仍不一致。")

        duplicate = read_one(base, "assets", ERRONEOUS_DUPLICATE, asset_fields)
        if links(duplicate["fields"].get("来源任务")) or text(duplicate["fields"].get("槽位")) or text(duplicate["fields"].get("运行ID")):
            raise FactoryError("误建重复 Asset 已出现任务关系，拒绝删除。")
        reviews = base.list_records("reviews", field_names=["图片资产"])
        if any(ERRONEOUS_DUPLICATE in links(r["fields"].get("图片资产")) for r in reviews):
            raise FactoryError("误建重复 Asset 已被审核记录引用，拒绝删除。")
        state.effect(
            f"demo-v2-repair-delete-duplicate:{base.token}:{ERRONEOUS_DUPLICATE}",
            lambda: True if not any(r["record_id"] == ERRONEOUS_DUPLICATE for r in base.list_records("assets", field_names=["资产ID"])) else None,
            lambda: base.delete_record("assets", ERRONEOUS_DUPLICATE),
        )
        if any(r["record_id"] == ERRONEOUS_DUPLICATE for r in base.list_records("assets", field_names=["资产ID"])):
            raise FactoryError("误建重复 Asset 删除后仍可读到。")
        print(json.dumps({"status":"PASS","first_asset":OLD_FIRST,"first_sha256":first_sha,"task_record_id":task["record_id"],"deleted_duplicate":ERRONEOUS_DUPLICATE,"asset_record_ids":desired_ids},ensure_ascii=False))
    finally:
        state.close()


if __name__ == "__main__":
    main()
