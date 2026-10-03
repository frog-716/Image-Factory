"""Read and admit at most one explicitly confirmed Feishu form record per poll."""
from __future__ import annotations

from .util import FactoryError, text


_BUSINESS_FIELDS = (
    "选择商品",
    "图片用途",
    "消费场景",
    "视觉风格",
    "数量",
    "附加要求",
)
_FORM_CREATE_CONFIRMATION = "确认创建演示任务"
_POLL_FIELDS = [*_BUSINESS_FIELDS, "创建确认", "运行ID", "提交", "系统状态"]
_PRODUCT_SELECTED_FILTER = {
    "logic": "and",
    "conditions": [["选择商品", "non_empty"]],
}


def _populated(value) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _confirmed_create(value) -> bool:
    return (
        isinstance(value, str) and value == _FORM_CREATE_CONFIRMATION
    ) or (
        isinstance(value, list) and len(value) == 1 and
        isinstance(value[0], str) and value[0] == _FORM_CREATE_CONFIRMATION
    )


def _form_drafts(rows) -> list[dict]:
    drafts = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("fields"), dict):
            raise FactoryError("表单任务列表包含无效记录，停止受理。")
        fields = row["fields"]
        if text(fields.get("运行ID")).strip():
            continue
        if not _populated(fields.get("选择商品")):
            continue
        if _populated(fields.get("系统状态")):
            continue
        # Feishu's single-select may be represented as a string or one selected
        # value. Accept only the exact, human-readable confirmation choice.
        if not _confirmed_create(fields.get("创建确认")):
            continue
        drafts.append(row)
    return drafts


def poll_form_once(engine, grant):
    """Admit one eligible form draft into Engine's queued Runtime.

    This function never advances Runtime or performs an image dispatch. Remote
    read/write errors are allowed to propagate so an uncertain write is not
    silently retried.
    """
    config = getattr(engine, "config", None)
    profile = config.get("intake_profile") if isinstance(config, dict) else None
    required_profile = {
        "allowed_product_ids", "workflow_id", "channel", "placement",
        "namespace", "mode", "review_policy_version", "max_calls",
    }
    if not isinstance(profile, dict) or not required_profile.issubset(profile):
        raise FactoryError("缺少受信表单受理配置，未扫描任务表。")
    if (not isinstance(grant, dict) or grant.get("approved") is not True or
            not text(grant.get("authorization_id")).strip() or
            not isinstance(grant.get("scope"), dict) or not grant["scope"]):
        raise FactoryError("缺少显式非空的受信授权，未扫描任务表。")

    rows = engine.base.list_records(
        "tasks",
        field_names=_POLL_FIELDS,
        filter_json=_PRODUCT_SELECTED_FILTER,
    )
    drafts = _form_drafts(rows)
    if not drafts:
        return {"status": "idle"}
    if len(drafts) != 1:
        raise FactoryError("发现多条待受理表单，停止自动选择。")

    row = drafts[0]
    task_id = row.get("record_id")
    if not isinstance(task_id, str) or not task_id:
        raise FactoryError("待受理表单缺少记录 ID，停止受理。")
    result = engine.admit_form_record(task_id, grant)
    return {
        "status": "admitted",
        "task_record_id": task_id,
        "run_id": result["run_id"],
        "admission": result,
    }
