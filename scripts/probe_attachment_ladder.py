#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import time
import zipfile
from pathlib import Path

from factory.cli import configuration
from factory.lark import FeishuGateway
from factory.state import State
from factory.util import FactoryError, UnknownWrite, file_hash, write_json


LEVELS = (
    ("P0", 48, ".bin"),
    ("P1", 10 * 1024, ".bin"),
    ("P2", 100 * 1024, ".bin"),
    ("P3", 500 * 1024, ".bin"),
    ("P4", 1024 * 1024, ".bin"),
    ("P5", None, ".png"),
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--state", default="var/live")
    parser.add_argument("--zip", default="TOPSTAR-Kids-Demo-Individual.zip")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cleanup", action="store_true")
    return parser.parse_args()


def attachment(base, record_id: str, name: str, size: int):
    rows = base.list_records("assets", field_names=["资产ID", "图片"])
    row = next((item for item in rows if item["record_id"] == record_id), None)
    if row is None:
        raise FactoryError("附件探针记录不存在。")
    matches = [item for item in (row["fields"].get("图片") or [])
               if item.get("name") == name and item.get("size") == size]
    if len(matches) > 1:
        raise FactoryError("附件探针出现重复附件。")
    return matches[0] if matches else None


def make_files(archive: Path, output: Path):
    with zipfile.ZipFile(archive) as bundle:
        original = bundle.read("01-indoor.png")
        manifest = json.loads(bundle.read("manifest.json"))
    expected = next(item["sha256"] for item in manifest["assets"] if item["file"] == "01-indoor.png")
    if hashlib.sha256(original).hexdigest() != expected:
        raise FactoryError("01-indoor.png 与 manifest SHA256 不一致。")
    output.mkdir(parents=True, exist_ok=True)
    files = []
    for level, size, suffix in LEVELS:
        data = original if size is None else original[:size]
        path = output / ("01-indoor.png" if level == "P5" else f"{level}-{len(data)}{suffix}")
        path.write_bytes(data)
        files.append((level, path))
    return files, expected


def main():
    args = parse_args()
    root = Path.cwd()
    output = root / "var" / "live" / "attachment-ladder" / args.run_id
    files, manifest_sha = make_files(root / args.zip, output)
    config = configuration(root / args.config)
    base = FeishuGateway(config)
    state = State(root / args.state)
    state.bind_base(base.token)
    marker = f"IF-ATTACHMENT-LADDER-{args.run_id}"
    report = {
        "run_id": args.run_id,
        "marker": marker,
        "manifest_sha256": manifest_sha,
        "cli_timeout_seconds": base.cli.timeout,
        "attachment_timeout_seconds": getattr(base.cli, "attachment_timeout", base.cli.timeout),
        "levels": [],
        "status": "running",
    }
    try:
        record = state.effect(
            f"attachment-ladder-create:{base.token}:{marker}",
            lambda: base.find_unique("assets", "资产ID", marker),
            lambda: base.create_record("assets", {
                "素材名": f"[IF] Attachment ladder {args.run_id}",
                "资产ID": marker,
                "类型": ["风格参考"],
                "示例数据": True,
                "允许用于生图": False,
                "来源说明": "附件分级能力探针；禁止用于商品生产",
                "授权说明": "诊断数据；完成后清理",
            }),
        )
        record_id = record["record_id"]
        report["record_id"] = record_id
        for level, path in files:
            item = {
                "level": level,
                "filename": path.name,
                "byte_length": path.stat().st_size,
                "original_sha256": file_hash(path),
                "upload_command_type": "lark-cli base +record-upload-attachment via FeishuGateway",
            }
            report["levels"].append(item)
            key = f"attachment-ladder-upload:{base.token}:{marker}:{level}:{item['original_sha256']}"
            started = time.monotonic()
            try:
                state.effect(
                    key,
                    lambda path=path: attachment(base, record_id, path.name, path.stat().st_size),
                    lambda path=path: base.upload_attachment("assets", record_id, "图片", path),
                )
                item["upload_exception"] = None
            except (FactoryError, UnknownWrite) as exc:
                item["upload_exception"] = type(exc).__name__
                item["upload_error"] = str(exc)
            item["upload_wall_seconds"] = round(time.monotonic() - started, 3)
            item["upload_cli"] = base.last_attachment_upload_diagnostic

            remote = attachment(base, record_id, path.name, path.stat().st_size)
            item["readback"] = {"present": remote is not None, "attachment": remote}
            if item["upload_exception"] is not None:
                item["status"] = "stopped_after_unknown_or_error"
                report["status"] = "stopped"
                report["first_failure"] = level
                break
            if remote is None:
                item["status"] = "readback_absent"
                report["status"] = "stopped"
                report["first_failure"] = level
                break

            destination = output / f"download-{path.name}"
            started = time.monotonic()
            try:
                base.download_attachment("assets", record_id, remote, destination)
                item["download_exception"] = None
            except FactoryError as exc:
                item["download_exception"] = type(exc).__name__
                item["download_error"] = str(exc)
            item["download_wall_seconds"] = round(time.monotonic() - started, 3)
            item["download_cli"] = base.cli.last_diagnostic
            if item["download_exception"] is not None or not destination.is_file():
                item["status"] = "download_failed"
                report["status"] = "stopped"
                report["first_failure"] = level
                break
            item["download_byte_length"] = destination.stat().st_size
            item["download_sha256"] = file_hash(destination)
            item["sha256_equal"] = item["download_sha256"] == item["original_sha256"]
            item["bytes_equal"] = item["download_byte_length"] == item["byte_length"]
            item["status"] = "pass" if item["sha256_equal"] and item["bytes_equal"] else "hash_mismatch"
            if item["status"] != "pass":
                report["status"] = "stopped"
                report["first_failure"] = level
                break
        else:
            report["status"] = "pass"

        if report["status"] == "pass" and args.cleanup:
            state.effect(
                f"attachment-ladder-delete:{base.token}:{marker}",
                lambda: True if base.find_unique("assets", "资产ID", marker) is None else None,
                lambda: base.delete_record("assets", record_id),
            )
            report["record_absent_after_delete"] = base.find_unique("assets", "资产ID", marker) is None
    finally:
        write_json(output / "report.json", report)
        state.close()
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
