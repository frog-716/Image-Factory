"""Loopback-only Human UI and trusted review Engine API.

The browser never receives Runtime, asset, hash, reviewer, policy, or revision
identifiers.  It receives opaque, short-lived capabilities.  The Engine
re-resolves and verifies all trusted fields immediately before the only remote
mutation, journals that mutation in the existing Runtime SQLite database, and
then writes through ``FeishuGateway``.
"""
from __future__ import annotations

import json
import mimetypes
import secrets
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .runtime import Blocked, Conflict, Runtime, digest
from .runtime_runner import runtime_db
from .util import FactoryError, UnknownWrite, confined, file_hash, integer, links, text


STATIC_ROOT = Path(__file__).resolve().parent / "human_ui_static"
_CANDIDATE_FIELDS = [
    "素材名", "资产ID", "图片", "来源任务", "SHA256", "示例数据", "槽位",
    "运行ID", "审核状态", "模式", "资产角色",
]
_REVIEW_FIELDS = [
    "审核标题", "图片资产", "目标SHA256", "结论", "审核人", "商品准确",
    "品牌及渠道检查", "人工确认", "原因", "Demo视觉检查", "仅限演示使用",
    "审核策略版本", "运行ID", "审核版本", "创建时间", "创建人",
]


@dataclass(frozen=True)
class _ReviewContext:
    token: str
    issued_at: float
    scene: str
    slot: str
    asset_id: str
    asset_record_id: str
    sha256: str
    local_path: str
    policy: str
    mode: str


@dataclass(frozen=True)
class _DeliveryContext:
    token: str
    issued_at: float
    task_record_id: str
    attachment: dict
    filename: str
    sha256: str


class HumanReviewEngine:
    """Trusted adapter between the minimal browser UI and Workflow Engine."""

    def __init__(
        self,
        base,
        state_root: Path,
        run_id: str,
        *,
        reviewer_id: str,
        context_ttl_seconds: int = 1800,
        on_review_confirmed: Callable[[], None] | None = None,
    ):
        if not isinstance(reviewer_id, str) or not reviewer_id.strip():
            raise FactoryError("受信飞书用户身份缺失，拒绝启动审核界面。")
        self.base = base
        self.state_root = Path(state_root).resolve()
        self.run_id = run_id
        self.reviewer_id = reviewer_id
        self.context_ttl_seconds = integer(
            context_ttl_seconds, "context_ttl_seconds", 60, 86400
        )
        self._contexts: dict[str, _ReviewContext] = {}
        self._deliveries: dict[str, _DeliveryContext] = {}
        self._on_review_confirmed = on_review_confirmed
        self._lock = threading.RLock()

    def _purge_expired(self) -> None:
        cutoff = time.time() - self.context_ttl_seconds
        self._contexts = {
            token: context
            for token, context in self._contexts.items()
            if context.issued_at >= cutoff
        }
        self._deliveries = {
            token: context
            for token, context in self._deliveries.items()
            if context.issued_at >= cutoff
        }

    def _runtime(self) -> Runtime:
        return Runtime(runtime_db(self.state_root))

    def _runtime_review_contract(self, runtime: Runtime) -> tuple[str, str]:
        status = runtime.status(self.run_id)
        nxt = status["next"]
        if status["state"] != "waiting_review" or nxt.get("kind") != "human_review":
            raise Blocked("当前任务已经不在待审核阶段；请刷新页面。")
        plan = runtime.plan(self.run_id)
        mode = plan.get("mode")
        step_id = nxt.get("step")
        step = next(
            (item for item in plan.get("steps", []) if item.get("id") == step_id), None
        )
        if not isinstance(step, dict) or step.get("kind") != "human_review":
            raise Blocked("当前 Runtime 没有可信审核步骤。")
        policy = step.get("policy")
        if mode == "demo" and policy != "demo-v2":
            raise Blocked("Demo 审核策略不是 Engine 支持的当前版本。")
        if mode == "production" and policy not in {"production-v1"}:
            raise Blocked("Production 审核策略不是 Engine 支持的当前版本。")
        return mode, policy

    def _candidate_rows(self, runtime: Runtime) -> list[dict]:
        mode, policy = self._runtime_review_contract(runtime)
        composed = runtime.step_output(self.run_id, "candidate-composition")
        raw = composed.get("candidates")
        if not isinstance(raw, dict) or not raw:
            raise Blocked("Runtime 没有当前候选输出。")
        task = self.base.find_unique_typed(
            "tasks",
            "任务名",
            self.run_id,
            field_names=["任务名", "运行ID", "命名空间", "模式", "审核策略版本", "系统状态"],
        )
        if not task:
            raise Blocked("飞书没有当前 Runtime 对应任务。")
        task_fields = task["fields"]
        if (
            text(task_fields.get("运行ID")) != self.run_id
            or text(task_fields.get("模式")) != mode
            or text(task_fields.get("审核策略版本")) != policy
        ):
            raise Blocked("飞书任务与当前 Runtime / Policy 不一致。")
        slots = {"outdoor": "A", "indoor": "B", "studio": "C"}
        rows = []
        for scene, value in sorted(raw.items(), key=lambda item: slots.get(item[0], item[0])):
            if not isinstance(value, dict) or not isinstance(value.get("output"), dict):
                raise Blocked("Runtime 候选输出结构不兼容。")
            output = value["output"]
            expected_sha = output.get("sha256")
            expected_path = output.get("local_path")
            slot = slots.get(scene)
            if not slot or not isinstance(expected_sha, str) or not isinstance(expected_path, str):
                raise Blocked("Runtime 候选缺少可信版本信息。")
            local = confined(self.state_root, expected_path)
            if file_hash(local) != expected_sha:
                raise Blocked("当前候选文件已经变化；旧页面已过期。")
            asset_id = f"{self.run_id}-CAND-{scene.upper()}"
            row = self.base.find_unique_typed(
                "assets", "资产ID", asset_id, field_names=_CANDIDATE_FIELDS
            )
            if not row:
                raise Blocked("飞书尚未投影当前候选。")
            fields = row["fields"]
            attachments = fields.get("图片") or []
            if (
                text(fields.get("运行ID")) != self.run_id
                or text(fields.get("SHA256")) != expected_sha
                or text(fields.get("模式")) != mode
                or text(fields.get("资产角色")) != "candidate"
                or text(fields.get("槽位")) != slot
                or links(fields.get("来源任务")) != [task["record_id"]]
                or len(attachments) != 1
                or not attachments[0].get("file_token")
            ):
                raise Blocked("飞书当前候选版本已经变化；旧页面已过期。")
            rows.append(
                {
                    "scene": scene,
                    "slot": slot,
                    "asset_id": asset_id,
                    "asset_record_id": row["record_id"],
                    "sha256": expected_sha,
                    "local_path": expected_path,
                    "policy": policy,
                    "mode": mode,
                    "attachment": attachments[0],
                }
            )
        return rows

    @staticmethod
    def _operation_id(candidate: dict) -> str:
        return "human_review_" + digest(
            {
                "run_id": candidate["run_id"],
                "asset_id": candidate["asset_id"],
                "sha256": candidate["sha256"],
            }
        )[:48]

    def _issue_context(self, candidate: dict) -> _ReviewContext:
        token = secrets.token_urlsafe(32)
        context = _ReviewContext(
            token=token,
            issued_at=time.time(),
            scene=candidate["scene"],
            slot=candidate["slot"],
            asset_id=candidate["asset_id"],
            asset_record_id=candidate["asset_record_id"],
            sha256=candidate["sha256"],
            local_path=candidate["local_path"],
            policy=candidate["policy"],
            mode=candidate["mode"],
        )
        self._contexts[token] = context
        return context

    def _context(self, token: str) -> _ReviewContext:
        if not isinstance(token, str) or len(token) < 32:
            raise Blocked("审核页面无效或已过期；请刷新。")
        context = self._contexts.get(token)
        if context is None or time.time() - context.issued_at > self.context_ttl_seconds:
            self._contexts.pop(token, None)
            raise Blocked("审核页面无效或已过期；请刷新。")
        return context

    def _current_candidate(self, context: _ReviewContext, runtime: Runtime) -> dict:
        current = next(
            (
                item
                for item in self._candidate_rows(runtime)
                if item["scene"] == context.scene and item["slot"] == context.slot
            ),
            None,
        )
        if current is None or any(
            current[key] != getattr(context, key)
            for key in (
                "asset_id", "asset_record_id", "sha256", "local_path", "policy", "mode"
            )
        ):
            raise Blocked("当前候选已经变化；旧页面已过期，请刷新后再审核。")
        return current

    def pending_reviews(self) -> dict:
        with self._lock:
            self._purge_expired()
            runtime = self._runtime()
            try:
                candidates = self._candidate_rows(runtime)
                items = []
                labels = {"outdoor": "户外", "indoor": "室内", "studio": "棚拍"}
                for candidate in candidates:
                    candidate = {**candidate, "run_id": self.run_id}
                    operation_id = self._operation_id(candidate)
                    write = runtime.write_status(operation_id)
                    if write and write["state"] == "confirmed":
                        continue
                    status = "processing" if write else "pending"
                    context = self._issue_context(candidate)
                    items.append(
                        {
                            "review_token": context.token,
                            "display": {
                                "title": f"{labels.get(context.scene, context.scene)}候选",
                                "slot": "候选 " + context.slot,
                                "alt": "待审核候选大图",
                                "image_url": "/api/v1/reviews/"
                                + urllib.parse.quote(context.token, safe="")
                                + "/image",
                            },
                            "status": status,
                        }
                    )
                runtime.audit(
                    self.run_id,
                    "human_ui_review_list_viewed",
                    {"actor_id": self.reviewer_id, "candidate_count": len(items)},
                )
                return {"items": items}
            finally:
                runtime.close()

    def review_image(self, token: str) -> Path:
        with self._lock:
            context = self._context(token)
            runtime = self._runtime()
            try:
                current = self._current_candidate(context, runtime)
                path = confined(self.state_root, current["local_path"])
                if file_hash(path) != current["sha256"]:
                    raise Blocked("当前候选已经变化；请刷新。")
                runtime.audit(
                    self.run_id,
                    "human_ui_candidate_opened",
                    {"actor_id": self.reviewer_id, "slot": context.slot},
                )
                return path
            finally:
                runtime.close()

    def _review_rows(self, asset_record_id: str) -> list[dict]:
        return [
            row
            for row in self.base.list_records("reviews", field_names=_REVIEW_FIELDS)
            if links(row["fields"].get("图片资产")) == [asset_record_id]
        ]

    def _next_revision(self, asset_record_id: str) -> int:
        maximum = 0
        for row in self._review_rows(asset_record_id):
            value = row["fields"].get("审核版本")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
                raise Blocked("已有审核缺少有效版本；需管理员核对。")
            maximum = max(maximum, int(value))
        return maximum + 1

    def _payload(self, candidate: dict, decision: str, reason: str, revision: int) -> dict:
        return {
            "kind": "human_review",
            "run_id": self.run_id,
            "asset_id": candidate["asset_id"],
            "asset_record_id": candidate["asset_record_id"],
            "sha256": candidate["sha256"],
            "mode": candidate["mode"],
            "policy": candidate["policy"],
            "revision": revision,
            "reviewer_id": self.reviewer_id,
            "decision": decision,
            "reason": reason,
        }

    def _fields(self, operation_id: str, payload: dict) -> dict:
        approved = payload["decision"] == "approved"
        mode = payload["mode"]
        return {
            "审核标题": "Human UI / " + operation_id,
            "图片资产": [payload["asset_record_id"]],
            "目标SHA256": payload["sha256"],
            "结论": ["通过" if approved else "驳回"],
            "审核人": [self.reviewer_id],
            "商品准确": approved and mode == "production",
            "品牌及渠道检查": approved and mode == "production",
            "人工确认": True,
            "原因": payload["reason"],
            "Demo视觉检查": approved and mode == "demo",
            "仅限演示使用": mode == "demo",
            "审核策略版本": payload["policy"],
            "运行ID": self.run_id,
            "审核版本": payload["revision"],
        }

    def _validate_remote(self, row: dict, operation_id: str, payload: dict) -> None:
        fields = row["fields"]
        expected = self._fields(operation_id, payload)
        if text(fields.get("审核标题")) != expected["审核标题"]:
            raise Conflict("审核写入读回标题不一致。")
        if links(fields.get("图片资产")) != expected["图片资产"]:
            raise Conflict("审核写入未绑定当前 Candidate。")
        if text(fields.get("目标SHA256")) != expected["目标SHA256"]:
            raise Conflict("审核写入未绑定当前 SHA256。")
        if text(fields.get("结论")) != text(expected["结论"]):
            raise Conflict("审核写入结论不一致。")
        if links(fields.get("审核人")) != [self.reviewer_id]:
            raise Conflict("审核写入的审核人不是当前受信用户。")
        if links(fields.get("创建人")) != [self.reviewer_id]:
            raise Conflict("飞书系统创建人与当前受信审核人不一致。")
        for name in (
            "商品准确", "品牌及渠道检查", "人工确认", "Demo视觉检查", "仅限演示使用"
        ):
            if fields.get(name) is not expected[name]:
                raise Conflict(f"审核写入字段 {name} 不一致。")
        if (
            text(fields.get("原因")) != expected["原因"]
            or text(fields.get("审核策略版本")) != expected["审核策略版本"]
            or text(fields.get("运行ID")) != expected["运行ID"]
            or integer(fields.get("审核版本"), "审核版本", 1, 1_000_000)
            != expected["审核版本"]
        ):
            raise Conflict("审核写入的 Runtime / Policy / Version 不一致。")
        if not fields.get("创建时间"):
            raise Conflict("飞书审核缺少系统创建时间。")

    def _find_operation(self, operation_id: str) -> dict | None:
        return self.base.find_unique_typed(
            "reviews",
            "审核标题",
            "Human UI / " + operation_id,
            field_names=_REVIEW_FIELDS,
        )

    def _confirm(self, runtime: Runtime, operation_id: str, payload: dict, row: dict) -> dict:
        self._validate_remote(row, operation_id, payload)
        payload_hash = digest(payload)
        receipt = {
            "kind": "human_review",
            "run_id": self.run_id,
            "record_id": row["record_id"],
            "operation_id": operation_id,
            "asset_id": payload["asset_id"],
            "sha256": payload["sha256"],
            "decision": payload["decision"],
            "revision": payload["revision"],
            "reviewer_id": self.reviewer_id,
            "payload_hash": payload_hash,
        }
        runtime.confirm_write(
            operation_id,
            expected_hash=payload_hash,
            observed_hash=digest(payload),
            receipt=receipt,
        )
        runtime.audit(
            self.run_id,
            "human_review_confirmed",
            {
                "operation_id": operation_id,
                "record_id": row["record_id"],
                "actor_id": self.reviewer_id,
                "decision": payload["decision"],
                "revision": payload["revision"],
                "payload_hash": payload_hash,
            },
        )
        if self._on_review_confirmed is not None:
            self._on_review_confirmed()
        return {"status": "reviewed", "decision": payload["decision"]}

    def _reconcile_existing(
        self, runtime: Runtime, operation_id: str, decision: str, reason: str
    ) -> dict | None:
        write = runtime.write_status(operation_id)
        if not write:
            return None
        if write["state"] == "confirmed":
            receipt = write.get("receipt") or {}
            if receipt.get("decision") != decision:
                raise Conflict("当前 Candidate 已有不同的有效审核，不能重复提交。")
            return {"status": "reviewed", "decision": receipt["decision"]}
        row = self._find_operation(operation_id)
        if row is None:
            return {"status": "processing", "decision": decision}
        fields = row["fields"]
        payload = {
            "kind": "human_review",
            "run_id": self.run_id,
            "asset_id": text(
                self.base.get_record(
                    "assets", links(fields.get("图片资产"))[0], ["资产ID"]
                )["fields"].get("资产ID")
            ),
            "asset_record_id": links(fields.get("图片资产"))[0],
            "sha256": text(fields.get("目标SHA256")),
            "mode": "demo" if text(fields.get("审核策略版本")) == "demo-v2" else "production",
            "policy": text(fields.get("审核策略版本")),
            "revision": integer(fields.get("审核版本"), "审核版本", 1, 1_000_000),
            "reviewer_id": self.reviewer_id,
            "decision": "approved" if text(fields.get("结论")) == "通过" else "rejected",
            "reason": text(fields.get("原因")),
        }
        if digest(payload) != write["payload_hash"] or payload["decision"] != decision or payload["reason"] != reason:
            raise Conflict("重复提交与已登记审核不一致。")
        return self._confirm(runtime, operation_id, payload, row)

    def submit_review(self, token: str, decision: str, reason: str = "") -> dict:
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision 只允许 approved 或 rejected")
        if not isinstance(reason, str):
            raise ValueError("reason 必须是文本")
        reason = reason.strip()
        if len(reason) > 500:
            raise ValueError("reason 最多 500 个字符")
        with self._lock:
            context = self._context(token)
            runtime = self._runtime()
            try:
                candidate = self._current_candidate(context, runtime)
                candidate["run_id"] = self.run_id
                operation_id = self._operation_id(candidate)
                existing = self._reconcile_existing(runtime, operation_id, decision, reason)
                if existing:
                    return existing
                # Verify the exact remote attachment bytes immediately before review creation.
                with tempfile.TemporaryDirectory(prefix="image-factory-review-") as directory:
                    downloaded = Path(directory) / "candidate.img"
                    self.base.download_attachment(
                        "assets", candidate["asset_record_id"], candidate["attachment"], downloaded
                    )
                    if file_hash(downloaded) != candidate["sha256"]:
                        runtime.audit(
                            self.run_id,
                            "human_review_rejected_stale",
                            {"actor_id": self.reviewer_id, "slot": context.slot},
                        )
                        raise Blocked("当前 Candidate 文件已经变化；旧页面已过期。")
                revision = self._next_revision(candidate["asset_record_id"])
                payload = self._payload(candidate, decision, reason, revision)
                runtime.begin_write(operation_id, payload)
                runtime.audit(
                    self.run_id,
                    "human_review_requested",
                    {
                        "operation_id": operation_id,
                        "actor_id": self.reviewer_id,
                        "decision": decision,
                        "revision": revision,
                        "payload_hash": digest(payload),
                    },
                )
                try:
                    created = self.base.create_record(
                        "reviews", self._fields(operation_id, payload)
                    )
                except UnknownWrite:
                    runtime.unknown_write(operation_id)
                    row = self._find_operation(operation_id)
                    if row is None:
                        runtime.audit(
                            self.run_id,
                            "human_review_remote_unknown",
                            {"operation_id": operation_id, "actor_id": self.reviewer_id},
                        )
                        return {"status": "processing", "decision": decision}
                    return self._confirm(runtime, operation_id, payload, row)
                row = self.base.get_record("reviews", created["record_id"], _REVIEW_FIELDS)
                return self._confirm(runtime, operation_id, payload, row)
            finally:
                runtime.close()

    def task_list(self) -> dict:
        with self._lock:
            self._purge_expired()
            runtime = self._runtime()
            try:
                status = runtime.status(self.run_id)
                labels = {
                    "queued": "排队中",
                    "running": "生产中",
                    "waiting_worker": "生产中",
                    "waiting_review": "待审核",
                    "delivery_pending_sync": "正在打包",
                    "completed": "已完成",
                    "blocked": "需要管理员核对",
                }
                delivery_url = None
                if status["state"] == "completed":
                    output = runtime.step_output(self.run_id, "approved-export")
                    expected_sha = output.get("zip_sha256")
                    expected_path = output.get("zip")
                    if isinstance(expected_sha, str) and isinstance(expected_path, str):
                        task = self.base.find_unique_typed(
                            "tasks", "任务名", self.run_id,
                            field_names=["任务名", "运行ID", "已批准交付包"],
                        )
                        if not task or text(task["fields"].get("运行ID")) != self.run_id:
                            raise Blocked("交付任务与当前 Runtime 不一致。")
                        filename = Path(expected_path).name
                        matches = [
                            item for item in (task["fields"].get("已批准交付包") or [])
                            if item.get("name") == filename and item.get("file_token")
                        ]
                        if len(matches) != 1:
                            raise Blocked("批准交付包尚未唯一回写。")
                        token = secrets.token_urlsafe(32)
                        self._deliveries[token] = _DeliveryContext(
                            token=token, issued_at=time.time(),
                            task_record_id=task["record_id"], attachment=matches[0],
                            filename=filename, sha256=expected_sha,
                        )
                        delivery_url = "/api/v1/deliveries/" + urllib.parse.quote(token, safe="")
                return {
                    "items": [
                        {
                            "display": {
                                "title": "童鞋图片任务",
                                "status": labels.get(status["state"], "处理中"),
                                "detail": "审核完成后，批准图片会自动进入交付包。",
                                "delivery_url": delivery_url,
                            }
                        }
                    ]
                }
            finally:
                runtime.close()

    def delivery_file(self, token: str) -> tuple[bytes, str]:
        with self._lock:
            self._purge_expired()
            context = self._deliveries.get(token)
            if context is None:
                raise Blocked("交付入口已过期，请刷新任务页面。")
            runtime = self._runtime()
            try:
                if runtime.status(self.run_id)["state"] != "completed":
                    raise Blocked("当前任务尚未完成交付。")
                with tempfile.TemporaryDirectory(prefix="image-factory-delivery-") as directory:
                    target = Path(directory) / context.filename
                    self.base.download_attachment(
                        "tasks", context.task_record_id, context.attachment, target
                    )
                    if file_hash(target) != context.sha256:
                        raise Conflict("交付包已变化，拒绝下载。")
                    content = target.read_bytes()
                runtime.audit(
                    self.run_id,
                    "human_ui_delivery_downloaded",
                    {"actor_id": self.reviewer_id, "sha256": context.sha256},
                )
                return content, context.filename
            finally:
                runtime.close()


class HumanUIApplication:
    def __init__(self, engine: HumanReviewEngine):
        self.engine = engine
        self.bootstrap_token = secrets.token_urlsafe(32)
        self.sessions: dict[str, str] = {}

    def create_session(self) -> tuple[str, str]:
        session = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        self.sessions[session] = csrf
        return session, csrf


class _Handler(BaseHTTPRequestHandler):
    server_version = "ImageFactoryHumanUI/1"

    @property
    def app(self) -> HumanUIApplication:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )
        self.send_header("Cache-Control", "no-store")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: dict) -> None:
        self._send(
            status,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _session(self) -> tuple[str, str] | None:
        cookies = {}
        for part in self.headers.get("Cookie", "").split(";"):
            if "=" in part:
                name, value = part.strip().split("=", 1)
                cookies[name] = value
        session = cookies.get("if_session")
        csrf = self.app.sessions.get(session or "")
        return (session, csrf) if session and csrf else None

    def _require_session(self, csrf: bool = False) -> tuple[str, str]:
        found = self._session()
        if not found:
            raise PermissionError("请从本机启动命令提供的入口重新打开。")
        if csrf and not secrets.compare_digest(
            self.headers.get("X-CSRF-Token", ""), found[1]
        ):
            raise PermissionError("页面安全令牌已失效，请刷新。")
        return found

    def _check_host(self) -> None:
        host = self.headers.get("Host", "").split(":", 1)[0]
        if host not in {"127.0.0.1", "localhost"}:
            raise PermissionError("只允许从本机回环地址访问。")
        origin = self.headers.get("Origin")
        if origin:
            parsed = urllib.parse.urlsplit(origin)
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
                raise PermissionError("拒绝跨站请求。")

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, PermissionError):
            status = HTTPStatus.FORBIDDEN
        elif isinstance(exc, ValueError):
            status = HTTPStatus.BAD_REQUEST
        elif isinstance(exc, (Blocked, Conflict)):
            status = HTTPStatus.CONFLICT
        elif isinstance(exc, UnknownWrite):
            status = HTTPStatus.SERVICE_UNAVAILABLE
        else:
            status = HTTPStatus.BAD_GATEWAY
        self._json(int(status), {"message": str(exc)})

    def do_GET(self) -> None:
        try:
            self._check_host()
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/" and "access" in query:
                supplied = query.get("access", [""])[0]
                if not secrets.compare_digest(supplied, self.app.bootstrap_token):
                    raise PermissionError("入口已失效。")
                session, _ = self.app.create_session()
                self.send_response(HTTPStatus.SEE_OTHER)
                self._security_headers()
                self.send_header(
                    "Set-Cookie",
                    f"if_session={session}; HttpOnly; SameSite=Strict; Path=/",
                )
                self.send_header("Location", "/")
                self.end_headers()
                return
            if parsed.path == "/api/v1/session":
                _, csrf = self._require_session()
                self._json(200, {"csrf": csrf})
                return
            if parsed.path == "/api/v1/reviews/pending":
                self._require_session()
                self._json(200, self.app.engine.pending_reviews())
                return
            if parsed.path == "/api/v1/tasks":
                self._require_session()
                self._json(200, self.app.engine.task_list())
                return
            delivery_prefix = "/api/v1/deliveries/"
            if parsed.path.startswith(delivery_prefix):
                self._require_session()
                token = urllib.parse.unquote(parsed.path[len(delivery_prefix) :]).strip("/")
                content, filename = self.app.engine.delivery_file(token)
                self.send_response(200)
                self._security_headers()
                self.send_header("Content-Type", "application/zip")
                self.send_header(
                    "Content-Disposition",
                    "attachment; filename*=UTF-8''" + urllib.parse.quote(filename, safe=""),
                )
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            prefix = "/api/v1/reviews/"
            if parsed.path.startswith(prefix) and parsed.path.endswith("/image"):
                self._require_session()
                token = urllib.parse.unquote(parsed.path[len(prefix) : -len("/image")]).strip("/")
                path = self.app.engine.review_image(token)
                self._send(200, path.read_bytes(), "image/png")
                return
            if parsed.path in {"/", "/index.html", "/app.js", "/styles.css"}:
                if parsed.path == "/":
                    self._require_session()
                name = "index.html" if parsed.path in {"/", "/index.html"} else parsed.path[1:]
                path = STATIC_ROOT / name
                if not path.is_file():
                    self._send(404, b"Not found", "text/plain; charset=utf-8")
                    return
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                if content_type.startswith("text/") or content_type == "application/javascript":
                    content_type += "; charset=utf-8"
                self._send(200, path.read_bytes(), content_type)
                return
            self._send(404, b"Not found", "text/plain; charset=utf-8")
        except Exception as exc:
            self._handle_error(exc)

    def do_POST(self) -> None:
        try:
            self._check_host()
            self._require_session(csrf=True)
            parsed = urllib.parse.urlsplit(self.path)
            prefix = "/api/v1/reviews/"
            suffix = "/decision"
            if not (parsed.path.startswith(prefix) and parsed.path.endswith(suffix)):
                self._send(404, b"Not found", "text/plain; charset=utf-8")
                return
            if self.headers.get_content_type() != "application/json":
                raise ValueError("只接受 application/json")
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 4096:
                raise ValueError("请求体大小无效")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict) or not set(body).issubset({"decision", "reason"}):
                raise ValueError("请求只允许 decision 和 reason")
            token = urllib.parse.unquote(parsed.path[len(prefix) : -len(suffix)]).strip("/")
            result = self.app.engine.submit_review(
                token, body.get("decision"), body.get("reason", "")
            )
            self._json(200 if result["status"] == "reviewed" else 202, result)
        except (UnicodeError, json.JSONDecodeError) as exc:
            self._handle_error(ValueError("JSON 请求体无效"))
        except Exception as exc:
            self._handle_error(exc)


def build_server(engine: HumanReviewEngine, host: str = "127.0.0.1", port: int = 0):
    if host != "127.0.0.1":
        raise FactoryError("Human UI V1 只允许绑定 127.0.0.1。")
    server = ThreadingHTTPServer((host, port), _Handler)
    server.app = HumanUIApplication(engine)  # type: ignore[attr-defined]
    return server


def serve_human_ui(base, state_root: Path, run_id: str, port: int = 8765, open_browser: bool = False):
    identity = base.current_user()
    reviewer_id = identity["open_id"]
    allowed = getattr(base, "config", {}).get("reviewer_open_ids", [])
    if allowed and reviewer_id not in allowed:
        raise FactoryError("当前已验证飞书用户不在允许审核人列表。")
    wake_lock = threading.Lock()

    def wake_runner() -> None:
        def worker() -> None:
            if not wake_lock.acquire(blocking=False):
                return
            try:
                from .runner_service import run_once
                from .state import State

                for _ in range(4):
                    state = State(state_root)
                    try:
                        if getattr(base, "token", None):
                            state.bind_base(base.token)
                        result = run_once(base, state, state_root, run_id)
                    finally:
                        state.close()
                    if result.get("state") in {"completed", "completed_demo", "waiting_worker"}:
                        break
                    if result.get("state") == "waiting_review" and result.get("review", {}).get("status") != "accepted":
                        break
            except Exception as exc:
                runtime = Runtime(runtime_db(state_root))
                try:
                    runtime.audit(
                        run_id,
                        "human_ui_runner_wake_deferred",
                        {"error_type": type(exc).__name__, "message": str(exc)[:300]},
                    )
                finally:
                    runtime.close()
            finally:
                wake_lock.release()

        threading.Thread(target=worker, name="human-ui-runner-wake", daemon=True).start()

    engine = HumanReviewEngine(
        base,
        state_root,
        run_id,
        reviewer_id=reviewer_id,
        on_review_confirmed=wake_runner,
    )
    server = build_server(engine, port=port)
    actual_port = server.server_address[1]
    entry = f"http://127.0.0.1:{actual_port}/?access={server.app.bootstrap_token}"  # type: ignore[attr-defined]
    if open_browser:
        webbrowser.open(entry)
    print(json.dumps({"entry": entry, "reviewer_source": "verified_lark_user"}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


__all__ = ["HumanReviewEngine", "build_server", "serve_human_ui"]
