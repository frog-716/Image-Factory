"""Pure, read-only presentation of Runtime state for a human operator.

The Runtime is the execution authority.  This module deliberately has no
transition methods and never opens the Runtime database: it accepts a status
mapping (or calls ``status(run_id)`` once on an injected read-only object) and
compresses that information into a small operator-facing model.

``allowed_actions`` are labels for work a person or a trusted external
adapter may perform.  They are not commands, callbacks, or Runtime mutation
APIs.  In particular, this layer cannot dispatch an image, finish a review,
change a budget, or mark a job successful.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


_KNOWN_RUN_STATES = {
    "queued",
    "running",
    "waiting_worker",
    "waiting_review",
    "delivery_pending_sync",
    "blocked",
    "completed",
}
_KNOWN_NEXT_STATES = {
    "pending",
    "waiting_worker",
    "waiting_review",
    "unknown",
    "blocked",
    "completed",
}
_VALID_MODES = {"demo", "production"}


@dataclass(frozen=True)
class HumanOpsContract:
    """Fixed user-facing workflow and field ownership contract.

    Names are stable machine identifiers; the UI may localize their labels,
    but it must not expose the bound fields as editable controls.
    """

    flow_phases: tuple[str, ...]
    human_task_fields: tuple[str, ...]
    automatic_task_fields: tuple[str, ...]
    review_visible_fields: tuple[str, ...]
    review_decisions: tuple[str, ...]
    review_reason_optional: bool
    trusted_bound_review_fields: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "flow_phases": list(self.flow_phases),
            "human_task_fields": list(self.human_task_fields),
            "automatic_task_fields": list(self.automatic_task_fields),
            "review_visible_fields": list(self.review_visible_fields),
            "review_decisions": list(self.review_decisions),
            "review_reason_optional": self.review_reason_optional,
            "trusted_bound_review_fields": list(self.trusted_bound_review_fields),
            "read_only_runtime": True,
            "ui_must_not_edit_trusted_bound_fields": True,
        }


OPERATOR_CONTRACT = HumanOpsContract(
    flow_phases=(
        "select_product",
        "create_task",
        "wait_for_production",
        "inspect_image",
        "review",
        "download",
    ),
    human_task_fields=("product", "purpose", "scene", "style", "quantity", "extra_requirements"),
    automatic_task_fields=(
        "workflow",
        "prompt_components",
        "mode",
        "run_id",
        "runtime_state",
        "budget",
        "source_snapshot",
        "authorization",
        "review_policy",
        "review_version",
    ),
    review_visible_fields=("large_image", "decision", "reason"),
    review_decisions=("approve", "reject"),
    review_reason_optional=True,
    trusted_bound_review_fields=(
        "sha256",
        "reviewer",
        "runtime",
        "run_id",
        "review_policy",
        "review_version",
        "created_at",
        "demo_visual_ok",
        "demo_use_only",
        "product_accuracy",
        "brand_channel_ok",
    ),
)


@dataclass(frozen=True)
class HumanOpBlocker:
    """A reason the operator cannot safely proceed automatically."""

    code: str
    message: str
    details: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": list(self.details)}


@dataclass(frozen=True)
class HumanOpView:
    """Immutable operator view; it contains no transition or write handle."""

    run_id: str
    mode: str
    phase: str
    runtime_state: str
    next_state: str
    next_step: str | None
    next_kind: str | None
    operator_phase: str
    budget_reserved: int
    budget_limit: int
    budget_remaining: int
    pending_outbox: int
    unknown_jobs: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    blockers: tuple[HumanOpBlocker, ...]
    missing_reviews: tuple[str, ...]
    next_instruction: str

    @property
    def is_blocked(self) -> bool:
        # Waiting for a person to review is an expected phase, even though
        # the view contains review blockers describing the missing work.
        return self.phase in {"blocked", "budget_exhausted"}

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-compatible data without exposing mutable internals."""

        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "phase": self.phase,
            "runtime_state": self.runtime_state,
            "next": {
                "state": self.next_state,
                "step": self.next_step,
                "kind": self.next_kind,
            },
            "operator_phase": self.operator_phase,
            "budget": {
                "reserved": self.budget_reserved,
                "limit": self.budget_limit,
                "remaining": self.budget_remaining,
            },
            "pending_outbox": self.pending_outbox,
            "unknown_jobs": list(self.unknown_jobs),
            "allowed_actions": list(self.allowed_actions),
            "blockers": [blocker.as_dict() for blocker in self.blockers],
            "missing_reviews": list(self.missing_reviews),
            "next_instruction": self.next_instruction,
            "read_only": True,
            "operator_contract": OPERATOR_CONTRACT.as_dict(),
        }

    # ``to_dict`` is a convenient spelling for callers rendering JSON.
    def to_dict(self) -> dict[str, Any]:
        return self.as_dict()


def _text(value: Any, fallback: str = "") -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _integer(value: Any, fallback: int = 0) -> int:
    # Runtime emits integers.  A whole-number float is tolerated for a
    # connector-shaped read model, but booleans and arbitrary text are not.
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return fallback


def _blocker(code: str, message: str, *details: str) -> HumanOpBlocker:
    return HumanOpBlocker(code, message, tuple(str(item) for item in details if str(item)))


def _append_unique(items: list[HumanOpBlocker], item: HumanOpBlocker) -> None:
    key = (item.code, item.message, item.details)
    if not any((old.code, old.message, old.details) == key for old in items):
        items.append(item)


def _review_details(
    status: Mapping[str, Any],
    run_id: str,
    mode: str,
    review: Mapping[str, Any] | None,
) -> tuple[list[HumanOpBlocker], tuple[str, ...]]:
    """Inspect a read-only review snapshot without accepting it."""

    blockers: list[HumanOpBlocker] = []
    if review is None:
        return [
            _blocker(
                "review_snapshot_missing",
                "尚未读取可信的人工作品审核快照；不能把待审核当作已批准。",
            )
        ], ("review_snapshot",)
    if not isinstance(review, Mapping):
        return [
            _blocker("review_snapshot_invalid", "审核快照不是可读取的对象。")
        ], ("review_snapshot",)
    # ``sync_review_to_runtime`` may wrap an accepted snapshot in a result
    # object.  Unwrap it without changing or persisting the supplied object.
    nested_snapshot = review.get("snapshot")
    if isinstance(nested_snapshot, Mapping):
        review = nested_snapshot
    elif (
        review.get("status") == "waiting"
        and isinstance(review.get("missing"), Sequence)
        and not isinstance(review.get("missing"), (str, bytes))
    ):
        missing = tuple(
            item for item in review.get("missing", ()) if isinstance(item, str) and item.strip()
        )
        return [
            _blocker("review_missing", "仍有候选没有人工审核事件，不能进入交付。", *missing)
        ], missing or ("review_snapshot",)

    review_mode = _text(review.get("mode"), "unknown")
    review_run_id = _text(review.get("run_id"), "unknown")
    if review_mode != mode:
        _append_unique(
            blockers,
            _blocker(
                "mode_scope_mismatch",
                "审核快照的 Demo/Production 模式与 Runtime 不一致，拒绝跨模式消费。",
                f"runtime={mode}",
                f"review={review_mode}",
            ),
        )
    if review_run_id != run_id:
        _append_unique(
            blockers,
            _blocker(
                "review_scope_mismatch",
                "审核快照不属于当前运行，不能用于当前交付。",
                f"runtime={run_id}",
                f"review={review_run_id}",
            ),
        )

    raw_candidates = review.get("candidates")
    if not isinstance(raw_candidates, Sequence) or isinstance(raw_candidates, (str, bytes)):
        return blockers + [
            _blocker("review_candidates_missing", "审核快照没有候选资产清单。")
        ], ("review_candidates",)
    candidates: list[tuple[str, str]] = []
    seen_candidates: set[str] = set()
    for candidate in raw_candidates:
        if not isinstance(candidate, Mapping):
            _append_unique(blockers, _blocker("review_candidate_invalid", "候选资产记录格式无效。"))
            continue
        asset_id = _text(candidate.get("asset_id"))
        asset_sha = _text(candidate.get("sha256"))
        if not asset_id or not asset_sha:
            _append_unique(
                blockers,
                _blocker("review_candidate_invalid", "候选缺少资产 ID 或 SHA256。"),
            )
            continue
        candidate_mode = candidate.get("mode")
        if candidate_mode is not None and _text(candidate_mode) != mode:
            _append_unique(
                blockers,
                _blocker(
                    "mode_scope_mismatch",
                    "候选资产的模式与当前 Runtime 不一致。",
                    asset_id,
                ),
            )
        if asset_id in seen_candidates:
            _append_unique(
                blockers,
                _blocker("review_scope_mismatch", "审核候选清单包含重复资产。", asset_id),
            )
            continue
        seen_candidates.add(asset_id)
        candidates.append((asset_id, asset_sha))

    raw_events = review.get("events")
    if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)):
        raw_events = []
    latest: dict[str, Mapping[str, Any]] = {}
    revisions: dict[tuple[str, int], Mapping[str, Any]] = {}
    for event in raw_events:
        if not isinstance(event, Mapping):
            _append_unique(blockers, _blocker("review_event_invalid", "审核事件格式无效。"))
            continue
        asset_id = _text(event.get("asset_id"))
        revision = event.get("revision")
        if not asset_id or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            _append_unique(blockers, _blocker("review_event_invalid", "审核事件缺少正整数版本或资产 ID."))
            continue
        key = (asset_id, revision)
        if key in revisions:
            _append_unique(
                blockers,
                _blocker("review_conflict", "同一资产的审核版本重复，需人工核对。", asset_id, str(revision)),
            )
            continue
        revisions[key] = event
        old = latest.get(asset_id)
        if old is None or revision > int(old.get("revision", 0)):
            latest[asset_id] = event

    candidate_ids = {asset_id for asset_id, _ in candidates}
    for asset_id in latest:
        if asset_id not in candidate_ids:
            _append_unique(
                blockers,
                _blocker("review_scope_mismatch", "审核事件包含不在当前候选集中的资产。", asset_id),
            )

    missing: list[str] = []
    required_checks = (
        ("demo_visual_ok", "demo_use_only")
        if mode == "demo"
        else ("product_accuracy", "brand_channel_ok")
    )
    for asset_id, asset_sha in candidates:
        event = latest.get(asset_id)
        if event is None:
            missing.append(asset_id)
            continue
        event_mode = _text(event.get("mode"), "unknown")
        event_run = _text(event.get("run_id"), "unknown")
        if event_mode != mode:
            _append_unique(
                blockers,
                _blocker("mode_scope_mismatch", "审核事件模式与当前 Runtime 不一致。", asset_id),
            )
        if event_run != run_id or event.get("sha256") != asset_sha:
            _append_unique(
                blockers,
                _blocker("review_scope_mismatch", "审核事件未绑定当前运行或当前图片 SHA256。", asset_id),
            )
        decision = _text(event.get("decision"), "unknown")
        if decision not in {"approve", "reject"}:
            _append_unique(
                blockers,
                _blocker("review_incomplete", "审核事件仍是待定/撤回状态，不能进入交付。", asset_id),
            )
            continue
        details: list[str] = []
        if not _text(event.get("actor_id")):
            details.append("actor_id")
        if event.get("human_confirmed") is not True:
            details.append("human_confirmed")
        # ``reason`` is an optional human-facing field.  The trusted review
        # adapter may bind its documented neutral fallback before handing a
        # snapshot to Runtime; the operator must not be forced to type one.
        if decision == "approve":
            details.extend(field for field in required_checks if event.get(field) is not True)
        if details:
            _append_unique(
                blockers,
                _blocker(
                    "review_incomplete",
                    "人工审核缺少必填确认项，不能由系统代为补齐。",
                    asset_id,
                    *details,
                ),
            )
    if missing:
        _append_unique(
            blockers,
            _blocker(
                "review_missing",
                "仍有候选没有人工审核事件，不能进入交付。",
                *missing,
            ),
        )
    return blockers, tuple(missing)


def _budget(status: Mapping[str, Any]) -> tuple[int, int, int, list[HumanOpBlocker]]:
    auth = status.get("authorization")
    auth = auth if isinstance(auth, Mapping) else {}
    maximum = _integer(status.get("image_calls_max"), _integer(auth.get("max_calls")))
    reserved = _integer(status.get("image_calls_reserved"), _integer(auth.get("reserved")))
    if "reserved" in auth:
        reserved = _integer(auth.get("reserved"), reserved)
    if "max_calls" in auth:
        maximum = _integer(auth.get("max_calls"), maximum)
    remaining = maximum - reserved
    blockers: list[HumanOpBlocker] = []
    if maximum < 0 or reserved < 0 or reserved > maximum:
        _append_unique(
            blockers,
            _blocker(
                "budget_invalid",
                "Runtime 报告的图片预算不一致，禁止根据不可信数字继续。",
                f"reserved={reserved}",
                f"limit={maximum}",
            ),
        )
    return reserved, maximum, remaining, blockers


def describe_run(
    status: Mapping[str, Any],
    *,
    review: Mapping[str, Any] | None = None,
) -> HumanOpView:
    """Build a read-only human-operation view from ``Runtime.status`` data.

    The optional ``review`` must already be a read-only snapshot supplied by a
    trusted review adapter.  This function only diagnoses missing or
    incomplete fields; it never treats them as an approval and never writes
    them back to Runtime.
    """

    if not isinstance(status, Mapping):
        status = {}
    run_id = _text(status.get("run_id"), "unknown-run")
    mode = _text(status.get("mode"), "unknown")
    runtime_state = _text(status.get("state"), "unknown")
    next_data = status.get("next")
    next_data = next_data if isinstance(next_data, Mapping) else {}
    next_state = _text(next_data.get("state"), runtime_state)
    next_step = _text(next_data.get("step")) or None
    next_kind = _text(next_data.get("kind")) or None
    unknown_jobs = tuple(
        item for item in (status.get("unknown_jobs") or ()) if isinstance(item, str) and item.strip()
    )
    pending_outbox = _integer(status.get("pending_outbox"))
    reserved, maximum, remaining, blockers = _budget(status)
    review_complete = False

    if mode not in _VALID_MODES:
        _append_unique(
            blockers,
            _blocker("mode_invalid", "Runtime 没有明确的 demo 或 production 隔离标记。"),
        )
    if runtime_state not in _KNOWN_RUN_STATES or next_state not in _KNOWN_NEXT_STATES:
        _append_unique(
            blockers,
            _blocker(
                "unknown_runtime_state",
                "Runtime 状态超出本适配器已知契约，必须人工核对后再继续。",
                runtime_state,
                next_state,
            ),
        )

    if unknown_jobs or runtime_state == "blocked" or next_state in {"unknown", "blocked"}:
        if unknown_jobs or next_state == "unknown":
            _append_unique(
                blockers,
                _blocker(
                    "unknown_attempt",
                    "存在结果未知的执行尝试；先人工核对是否已消耗额度，禁止自动重试。",
                    *unknown_jobs,
                ),
            )
        elif runtime_state == "blocked":
            _append_unique(
                blockers,
                _blocker("runtime_blocked", "Runtime 已阻塞，不能从人类操作层猜测下一状态。"),
            )

    if runtime_state == "waiting_review" or next_state == "waiting_review":
        review_blockers, missing_reviews = _review_details(status, run_id, mode, review)
        for item in review_blockers:
            _append_unique(blockers, item)
        review_blocker_codes = {
            "review_snapshot_missing",
            "review_candidates_missing",
            "review_candidate_invalid",
            "review_event_invalid",
            "review_conflict",
            "review_missing",
            "review_incomplete",
            "mode_scope_mismatch",
            "review_scope_mismatch",
        }
        review_complete = (
            review is not None
            and not missing_reviews
            and not any(item.code in review_blocker_codes for item in review_blockers)
        )
    else:
        missing_reviews = ()

    if next_state == "pending" and next_kind == "image_generate" and remaining <= 0:
        _append_unique(
            blockers,
            _blocker(
                "budget_exhausted",
                "项目图片工具预算已耗尽；不得自动重试、扩容或改走 API。",
                f"reserved={reserved}",
                f"limit={maximum}",
            ),
        )
    elif runtime_state == "waiting_worker" and remaining <= 0:
        _append_unique(
            blockers,
            _blocker(
                "budget_exhausted",
                "当前 worker 结果仍可登记，但已无预算进行新的图片尝试。",
                f"reserved={reserved}",
                f"limit={maximum}",
            ),
        )

    # Scope mismatches make even a nominally waiting-review run unsafe.  A
    # missing review remains waiting_review because a human can still supply
    # the missing external record.
    scope_codes = {
        "budget_invalid",
        "mode_invalid",
        "mode_scope_mismatch",
        "review_scope_mismatch",
        "unknown_runtime_state",
    }
    scope_blocked = any(item.code in scope_codes for item in blockers)
    if scope_blocked:
        phase = "blocked"
    elif runtime_state == "completed" or next_state == "completed":
        phase = "completed"
    elif runtime_state == "blocked" or next_state in {"blocked", "unknown"} or unknown_jobs:
        phase = "blocked"
    elif runtime_state == "waiting_review" or next_state == "waiting_review":
        phase = "blocked" if any(item.code == "mode_scope_mismatch" for item in blockers) else "waiting_review"
    elif runtime_state == "delivery_pending_sync":
        phase = "delivery_pending_sync"
    elif runtime_state == "waiting_worker" or next_state == "waiting_worker":
        phase = "waiting_worker"
    elif next_state == "pending" and next_kind == "image_generate" and remaining <= 0:
        phase = "budget_exhausted"
    elif runtime_state in {"queued", "running"}:
        phase = runtime_state
    else:
        phase = "blocked"

    if phase == "waiting_worker":
        allowed = ("wait_for_production",)
        instruction = "图片正在生产；普通用户只需等待，系统不会从界面重派或自动重试。"
    elif phase == "waiting_review":
        allowed = ("review_candidates",)
        if missing_reviews:
            instruction = "打开候选大图完成人工审核，逐张选择通过或退回；原因可选，技术字段由系统绑定。"
        else:
            instruction = "审核已提交，系统正在核对并准备交付；不会替人批准。"
    elif phase == "delivery_pending_sync":
        allowed = ("wait_for_delivery",)
        instruction = "审核已完成，系统正在打包批准图片；完成后会出现下载入口。"
    elif phase == "completed":
        allowed = ("download_approved_images",)
        instruction = "流程已完成；下载已批准图片。"
    elif phase == "budget_exhausted":
        allowed = ("contact_admin",)
        instruction = "图片额度不足；不能自动重试、扩容或改走付费 API，请联系管理员。"
    elif phase == "blocked":
        if any(item.code == "unknown_attempt" for item in blockers):
            allowed = ("contact_admin",)
            instruction = "生产结果需要管理员核对；普通用户不要重试。"
        elif any(item.code == "mode_scope_mismatch" for item in blockers):
            allowed = ("contact_admin",)
            instruction = "任务范围需要管理员核对；Demo 与 Production 数据不会混用。"
        else:
            allowed = ("contact_admin",)
            instruction = "任务状态需要管理员核对；系统已保留现场，不会自动继续。"
    else:
        allowed = ("wait_for_production",)
        instruction = "任务已提交；等待系统进入下一阶段。"

    if phase in {"queued", "running", "waiting_worker"}:
        operator_phase = "wait_for_production"
    elif phase == "waiting_review":
        # The same human review surface presents the large image first and
        # only then exposes its pass/return controls.
        operator_phase = "review" if review_complete else "inspect_image"
    elif phase == "delivery_pending_sync":
        operator_phase = "download"
    elif phase == "completed":
        operator_phase = "download"
    elif phase == "budget_exhausted":
        operator_phase = "create_task"
    else:
        operator_phase = "wait_for_production"

    return HumanOpView(
        run_id=run_id,
        mode=mode,
        phase=phase,
        runtime_state=runtime_state,
        next_state=next_state,
        next_step=next_step,
        next_kind=next_kind,
        operator_phase=operator_phase,
        budget_reserved=reserved,
        budget_limit=maximum,
        budget_remaining=remaining,
        pending_outbox=pending_outbox,
        unknown_jobs=unknown_jobs,
        allowed_actions=allowed,
        blockers=tuple(blockers),
        missing_reviews=missing_reviews,
        next_instruction=instruction,
    )


def describe_runtime(
    runtime: Any,
    run_id: str,
    *,
    review: Mapping[str, Any] | None = None,
) -> HumanOpView:
    """Read ``runtime.status(run_id)`` once and adapt it without mutation."""

    return describe_run(runtime.status(run_id), review=review)


def operator_contract() -> HumanOpsContract:
    """Return the immutable fixed UI/ownership contract."""

    return OPERATOR_CONTRACT


__all__ = [
    "HumanOpBlocker",
    "HumanOpView",
    "HumanOpsContract",
    "OPERATOR_CONTRACT",
    "describe_run",
    "describe_runtime",
    "operator_contract",
]
