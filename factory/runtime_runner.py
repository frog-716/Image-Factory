"""Local V1 runner adapter.

The adapter advances deterministic local nodes and stages real native-image
files.  It never invokes an image provider, approves an image, writes Feishu,
or falls back to an API.  A successful dispatch is the atomic budget boundary;
the caller then performs exactly one native image-tool call for that job.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

from .image_pipeline import compose_candidates, derive_white_preview, validate_product
from .media import inspect_image
from .runtime import Blocked, Conflict, Runtime, canonical, digest
from .util import FactoryError, atomic_write, confined, file_hash, now, read_json, safe_id, write_json

TOPSTAR_5056_PRODUCT_RECORD_ID = "recvwpu2KAjcoi"
TOPSTAR_5056_SOURCE_ASSET_RECORD_ID = "recvwpuAQYH9XZ"
TOPSTAR_5056_SOURCE_SHA256 = "13121c27e357b425cae73d25706509ebe59fb59790da6ac1481e49eeb845907b"
TOPSTAR_5056_CROSSCHECK_ASSET_RECORD_ID = "recvwpI4lp0htB"
TOPSTAR_5056_CROSSCHECK_SHA256 = "3bb5a1100e0780037eac7f8c8ed67c7a8aead53e360038dc28b1296803a29616"
TOPSTAR_5056_AUTH_SCOPE = {
    "namespace": "V1-DEMO-KIDS",
    "category": "kids_shoes",
    "mode": "demo",
    "product_record_id": TOPSTAR_5056_PRODUCT_RECORD_ID,
    "grant": "user-confirmed-topstar-5056-20260927",
}
TOPSTAR_5056_GRANT_SOURCE = "user-confirmed-topstar-5056-20260927"


def runtime_db(state_root: Path) -> Path:
    return state_root.resolve()/"ledger.sqlite3"


def create_demo(state_root: Path, template: Path, run_id: str, authorization_id: str,
                actor_id: str, limit: int = 6) -> dict:
    safe_id(run_id);safe_id(authorization_id);safe_id(actor_id)
    if not run_id.startswith("V1-DEMO-KIDS-"):
        raise Blocked("V1 Demo run_id 必须使用 V1-DEMO-KIDS- 前缀。")
    if limit != 6:
        raise Blocked("本轮项目级图片工具授权上限固定为 6。")
    plan=read_json(template);plan["run_id"]=run_id
    expected_operation={
        "kids-background-v1":"compose_three_candidates_same_source",
        "kids-background-v2":"compose_three_candidates_grounded_v2",
    }.get(plan.get("workflow_version"))
    composition=[step for step in plan.get("steps",[]) if step.get("id")=="candidate-composition"]
    if expected_operation is None or len(composition)!=1 or composition[0].get("operation")!=expected_operation:
        raise Blocked("Demo 合成操作必须与冻结的 Workflow 版本一致。")
    scope={"namespace":"V1-DEMO-KIDS","category":"kids_shoes","mode":"demo",
           "grant":"user-launch-2026-09-21"}
    plan["authorization_id"]=authorization_id;plan["authorization_scope"]=scope
    grant={"authorization_id":authorization_id,"scope":scope,"project_image_calls_limit":limit,
           "approved":True,"actor_id":actor_id,"source":"user launch message"}
    runtime=Runtime(runtime_db(state_root),authorization=grant,single_instance=True)
    try:
        created=runtime.create(plan,authorization=grant)
        write_json(state_root.resolve()/"active-v1-run.json",{"run_id":run_id,"authorization_id":authorization_id})
        return {"result":created,"run":runtime.status(run_id),"plan_hash":digest(plan)}
    finally:
        runtime.close()


def create_asset_batch(state_root: Path, template: Path, run_id: str,
                       authorization_id: str, actor_id: str, reference_image: Path) -> dict:
    """Freeze one separately authorized, local-only fictional asset batch."""
    safe_id(run_id);safe_id(authorization_id);safe_id(actor_id)
    if not run_id.startswith("FICTIONAL-ASSET-"):
        raise Blocked("独立虚构素材批次必须使用 FICTIONAL-ASSET- 前缀。")
    _, source_info = validate_product(reference_image)
    state_root = state_root.resolve()
    snapshot = state_root/"references"/(run_id+"-source.png")
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.is_symlink():
        raise Blocked("批次参考图快照不能是软链接。")
    if snapshot.exists() and file_hash(snapshot) != source_info["sha256"]:
        raise Conflict("批次参考图快照与现有文件冲突。")
    if not snapshot.exists():
        shutil.copyfile(reference_image, snapshot)
    if file_hash(snapshot) != source_info["sha256"]:
        raise Blocked("参考图在建立快照期间发生变化。")
    plan = read_json(template)
    slots = plan.get("max_image_calls")
    if type(slots) is not int or not 1 <= slots <= 6:
        raise Blocked("本次独立素材授权最多 6 次图片调用。")
    steps = plan.get("steps", [])
    if len(steps) != slots or any(step.get("kind") != "image_generate" or
                               step.get("output_role") not in {"product_rgba", "catalog_image"} or
                               step.get("independent_image_count") != 1 for step in steps):
        raise Blocked("素材批次必须仅包含额度内的独立商品图槽位。")
    reference = {"local_path": str(snapshot), "sha256": source_info["sha256"],
                 "source": "caller_supplied_local_reference"}
    for step in steps:
        step["reference_asset"] = reference
    scope = {"namespace": "FICTIONAL-ASSET-LAB", "category": "kids_shoes",
             "mode": "demo", "grant": "user-separate-batch-2026-09-27"}
    plan["run_id"] = run_id
    plan["authorization_id"] = authorization_id
    plan["authorization_scope"] = scope
    grant = {"authorization_id": authorization_id, "scope": scope,
             "project_image_calls_limit": 6, "approved": True, "actor_id": actor_id,
             "source": "user explicit separate fictional batch authorization 2026-09-27"}
    runtime = Runtime(runtime_db(state_root), authorization=grant, single_instance=True)
    try:
        created = runtime.create(plan, authorization=grant)
        return {"result": created, "run": runtime.status(run_id),
                "plan_hash": digest(plan), "reference_asset": reference}
    finally:
        runtime.close()


def create_topstar_source_batch(state_root: Path, run_id: str, authorization_id: str,
                                actor_id: str, reference_image: Path) -> dict:
    """Freeze the first TOPSTAR 5056 source cutout job without dispatching it."""
    safe_id(run_id)
    safe_id(authorization_id)
    safe_id(actor_id)
    if not run_id.startswith("TOPSTAR-SOURCE-5056-"):
        raise Blocked("TOPSTAR source run_id 必须使用 TOPSTAR-SOURCE-5056- 前缀。")
    if not authorization_id.startswith("AUTH-TOPSTAR-5056-BEIGE-"):
        raise Blocked("授权 ID 必须使用新的 AUTH-TOPSTAR-5056-BEIGE- 前缀。")

    from .media import inspect_image

    source_info = inspect_image(reference_image)
    if (source_info["format"] != "JPEG" or
            source_info["sha256"] != TOPSTAR_5056_SOURCE_SHA256):
        raise Blocked("仅接受已核实的 TOPSTAR 5056 Rustans JPEG 源图及冻结 SHA256。")

    state_root = state_root.resolve()
    snapshot_relative = f"references/{run_id}-source.jpg"
    snapshot = confined(state_root, snapshot_relative, must_exist=False)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.is_symlink():
        raise Blocked("TOPSTAR source 快照不能是软链接。")

    snapshot_identity = None

    def remove_own_incomplete_snapshot():
        if snapshot_identity is None:
            return
        try:
            current = snapshot.lstat()
        except FileNotFoundError:
            return
        if (stat.S_ISREG(current.st_mode) and
                (current.st_dev, current.st_ino) == snapshot_identity):
            try:
                snapshot.unlink()
            except OSError:
                pass

    if snapshot.exists():
        if not snapshot.is_file() or file_hash(snapshot) != TOPSTAR_5056_SOURCE_SHA256:
            raise Conflict("TOPSTAR source 快照与已冻结源图冲突。")
    else:
        try:
            with reference_image.open("rb") as source, snapshot.open("xb") as target:
                owned = os.fstat(target.fileno())
                snapshot_identity = (owned.st_dev, owned.st_ino)
                shutil.copyfileobj(source, target)
        except FileExistsError:
            if snapshot.is_symlink() or not snapshot.is_file() or file_hash(snapshot) != TOPSTAR_5056_SOURCE_SHA256:
                raise Conflict("TOPSTAR source 快照已由其他内容占用。")
        except BaseException:
            remove_own_incomplete_snapshot()
            raise

    snapshot_sha256 = file_hash(snapshot)
    source_sha256_after_copy = file_hash(reference_image)
    if snapshot_sha256 != TOPSTAR_5056_SOURCE_SHA256:
        remove_own_incomplete_snapshot()
        raise Blocked("state/references 快照在复制期间发生变化。")
    if source_sha256_after_copy != TOPSTAR_5056_SOURCE_SHA256:
        raise Blocked("源图或 state/references 快照在复制期间发生变化。")

    template_path = Path(__file__).resolve().parents[1] / "templates" / "runtime-topstar-5056-source-v1.json"
    plan = read_json(template_path)
    steps = plan.get("steps")
    if (plan.get("max_image_calls") != 1 or not isinstance(steps, list) or len(steps) != 1 or
            steps[0].get("kind") != "image_generate" or
            steps[0].get("output_role") != "product_rgba" or
            steps[0].get("independent_image_count") != 1):
        raise Blocked("TOPSTAR source Workflow 必须恰有一个 product_rgba 图片调用槽。")

    reference = {
        "local_path": str(snapshot),
        "relative_path": snapshot_relative,
        "sha256": TOPSTAR_5056_SOURCE_SHA256,
        "format": "JPEG",
        "product_record_id": TOPSTAR_5056_PRODUCT_RECORD_ID,
        "asset_id": TOPSTAR_5056_SOURCE_ASSET_RECORD_ID,
        "purpose": "same-view product reference for transparent background removal",
        "identity_crosscheck_asset_id": TOPSTAR_5056_CROSSCHECK_ASSET_RECORD_ID,
        "identity_crosscheck_sha256": TOPSTAR_5056_CROSSCHECK_SHA256,
    }
    steps[0]["reference_asset"] = reference
    plan["run_id"] = run_id
    plan["authorization_id"] = authorization_id
    plan["authorization_scope"] = dict(TOPSTAR_5056_AUTH_SCOPE)

    grant = {
        "authorization_id": authorization_id,
        "scope": dict(TOPSTAR_5056_AUTH_SCOPE),
        "project_image_calls_limit": 6,
        "approved": True,
        "actor_id": actor_id,
        "source": TOPSTAR_5056_GRANT_SOURCE,
    }
    runtime = Runtime(runtime_db(state_root), authorization=grant, single_instance=True)
    try:
        created = runtime.create(plan, authorization=grant)
        registered_authorization = runtime.registered_dispatch_authorization(run_id)
        if registered_authorization["actor_id"] != actor_id:
            raise Conflict("授权 ID 已绑定不同的受信操作人，禁止生成不一致的审计记录。")
        if registered_authorization["plan_hash"] != digest(plan):
            raise Conflict("TOPSTAR source plan 与已注册派发授权不一致。")
        status = runtime.status(run_id)
        if status["image_calls_reserved"] != 0 or status["authorization"]["reserved"] != 0:
            raise Conflict("初始化 TOPSTAR source run 时发现已有预算预留；停止，不进行派发。")
        plan_hash = digest(plan)
    finally:
        runtime.close()

    audit_path = confined(state_root, f"audit/{run_id}-pre-dispatch.json", must_exist=False)
    audit = {
        "schema_version": 1,
        "status": "ready_not_dispatched",
        "run_id": run_id,
        "plan_hash": plan_hash,
        "run_image_calls_limit": 1,
        "image_calls_reserved_at_initialization": 0,
        "authorization": {
            "authorization_id": authorization_id,
            "actor_id": actor_id,
            "source": TOPSTAR_5056_GRANT_SOURCE,
            "scope": dict(TOPSTAR_5056_AUTH_SCOPE),
            "project_image_calls_limit": 6,
            "reserved_at_initialization": 0,
        },
        "reference_image": {
            "product_record_id": TOPSTAR_5056_PRODUCT_RECORD_ID,
            "asset_id": TOPSTAR_5056_SOURCE_ASSET_RECORD_ID,
            "local_path": snapshot_relative,
            "format": "JPEG",
            "source_sha256_before_copy": source_info["sha256"],
            "snapshot_sha256": snapshot_sha256,
            "source_sha256_after_copy": source_sha256_after_copy,
            "tool_input": True,
        },
        "identity_crosscheck": {
            "asset_id": TOPSTAR_5056_CROSSCHECK_ASSET_RECORD_ID,
            "sha256": TOPSTAR_5056_CROSSCHECK_SHA256,
            "tool_input": False,
            "note": "Urban 5056 image is a frozen visual identity cross-check only; the Rustans page SKU is not standalone SKU evidence.",
        },
        "dispatch_created": False,
    }
    if audit_path.is_symlink():
        raise Blocked("TOPSTAR pre-dispatch 审计文件不能是软链接。")
    if audit_path.exists():
        if not audit_path.is_file() or read_json(audit_path) != audit:
            raise Conflict("TOPSTAR pre-dispatch 审计文件已有不同内容，禁止覆盖。")
    else:
        write_json(audit_path, audit)

    return {
        "result": created,
        "run": status,
        "plan_hash": plan_hash,
        "reference_asset": reference,
        "audit_file": str(audit_path),
    }


def _job_dir(state_root: Path, job_id: str) -> Path:
    return state_root.resolve()/"runtime-jobs"/safe_id(job_id)


def materialize_job(state_root: Path, runtime: Runtime, job_id: str) -> Path:
    job=runtime.job(safe_id(job_id));request=json.loads(job["request"])
    root=_job_dir(state_root,job_id);root.mkdir(parents=True,exist_ok=True)
    payload={"contract_version":1,"job_id":job_id,"attempt_id":job["attempt_id"],
             "request_hash":job["request_hash"],"origin":job["origin"],"request":request,
             "output_directory":str(root/"output"),
             "rule":"调用一次已授权的 Codex 原生图片工具；禁止 API 回退、拼图、库存图或自动重试。"}
    path=root/"job.json";write_json(path,payload);return path


def dispatch_next(state_root: Path, run_id: str) -> dict:
    runtime=Runtime(runtime_db(state_root),single_instance=True)
    try:
        nxt=runtime.next(run_id)
        if nxt.get("state")!="pending" or nxt.get("kind")!="image_generate":
            raise Blocked("下一节点不是可派发的 image_generate；先运行 runtime-advance 或处理当前等待项。")
        definition=next(step for step in runtime.plan(run_id)["steps"] if step["id"]==nxt["step"])
        reference=definition.get("reference_asset")
        if reference:
            path=Path(reference["local_path"])
            if path.is_symlink() or not path.is_file() or file_hash(path)!=reference["sha256"]:
                raise Blocked("参考图快照丢失或已变化，禁止预留图片额度。")
        job=runtime.dispatch_registered(run_id,nxt["step"],origin="native")
        path=materialize_job(state_root,runtime,job["job_id"])
        return {"run_id":run_id,"step":nxt["step"],"job_id":job["job_id"],
                "attempt_id":job["attempt_id"],"request_hash":job["request_hash"],
                "job_file":str(path),"budget":runtime.status(run_id)["authorization"]}
    finally:
        runtime.close()


def stage_image(state_root: Path, job_id: str, image_path: Path, evidence_path: Path,
                actual_model: str, call_id: str) -> dict:
    if not actual_model.strip():
        raise Blocked("工具未报告模型时使用 not_reported，不能留空。")
    if not call_id.strip():
        raise Blocked("工具未报告调用 ID 时使用 not_reported，不能留空。")
    if image_path.is_symlink() or not image_path.is_file():
        raise Blocked("原生输出必须是普通图片文件。")
    if evidence_path.is_symlink() or not evidence_path.is_file() or not 0<evidence_path.stat().st_size<=200000:
        raise Blocked("工具证据必须是 1 至 200000 字节的普通文件。")
    runtime=Runtime(runtime_db(state_root),single_instance=True)
    try:
        job=runtime.job(safe_id(job_id));request=json.loads(job["request"])
        definition=request["definition"];role=definition.get("output_role")
        info=inspect_image(image_path)
        if role=="product_rgba":
            _,alpha=validate_product(image_path);info["alpha"]=alpha
        elif role=="background" and info["width"]!=info["height"]:
            raise Blocked("背景必须是独立正方形单图。")
        root=_job_dir(state_root,job_id);output_dir=root/"output";output_dir.mkdir(parents=True,exist_ok=True)
        suffix={"PNG":"png","JPEG":"jpg","WEBP":"webp"}[info["format"]]
        target=output_dir/(role+"."+suffix)
        if target.exists() and file_hash(target)!=file_hash(image_path):
            raise Conflict("同一 Job 已有不同输出，禁止覆盖。")
        if image_path.resolve()!=target.resolve():
            shutil.copyfile(image_path,target)
        evidence=root/"tool-evidence.txt"
        if evidence.exists() and file_hash(evidence)!=file_hash(evidence_path):
            raise Conflict("同一 Job 已有不同工具证据。")
        if evidence_path.resolve()!=evidence.resolve():
            shutil.copyfile(evidence_path,evidence)
        rel=str(target.relative_to(state_root.resolve()))
        receipt={"job_id":job_id,"attempt_id":job["attempt_id"],"request_hash":job["request_hash"],
                 "mode":request["mode"],"status":"succeeded","origin":"native",
                 "tool":"codex_native_image","model_reported":actual_model,
                 "provider_call_id":call_id,"generated_at":now(),
                 "evidence_ref":str(evidence.relative_to(state_root.resolve())),
                 "evidence_sha256":file_hash(evidence),
                 "outputs":[{"name":target.name,"sha256":file_hash(target),"bytes":target.stat().st_size,
                             "local_path":rel,"role":role}]}
        write_json(root/"result.json",receipt)
        accepted=runtime.accept(job_id,receipt,{target.name:target})
        return {"result":accepted,"job_id":job_id,"step":job["step_id"],
                "output":receipt["outputs"][0],"next":runtime.next(job["run_id"])}
    finally:
        runtime.close()


def reject_image(state_root: Path, job_id: str, image_path: Path, evidence_path: Path,
                 reason: str) -> dict:
    """Preserve an unusable native result and close its spent attempt.

    This is intentionally separate from ``stage_image``: a validation failure
    proves that an output exists, so the attempt is not an unknown/no-effect
    case and its reservation is never refunded or reused.
    """
    if not reason.strip():
        raise Blocked("必须说明明确失败原因。")
    if image_path.is_symlink() or not image_path.is_file():
        raise Blocked("失败输出也必须是普通图片文件。")
    if evidence_path.is_symlink() or not evidence_path.is_file() or not 0<evidence_path.stat().st_size<=200000:
        raise Blocked("工具证据必须是 1 至 200000 字节的普通文件。")
    runtime=Runtime(runtime_db(state_root),single_instance=True)
    try:
        job=runtime.job(safe_id(job_id))
        if job["state"]!="waiting":
            raise Blocked("只有 waiting Job 可登记明确失败。")
        root=_job_dir(state_root,job_id);failed=root/"failed";failed.mkdir(parents=True,exist_ok=True)
        image_target=failed/("output"+image_path.suffix.lower())
        evidence_target=failed/"tool-evidence.txt"
        for source,target in ((image_path,image_target),(evidence_path,evidence_target)):
            if target.exists() and file_hash(target)!=file_hash(source):
                raise Conflict("同一失败 Job 已保存不同证据，禁止覆盖。")
            if source.resolve()!=target.resolve():
                shutil.copyfile(source,target)
        record={"reason":reason.strip(),
                "image_path":str(image_target.relative_to(state_root.resolve())),
                "image_sha256":file_hash(image_target),
                "evidence_path":str(evidence_target.relative_to(state_root.resolve())),
                "evidence_sha256":file_hash(evidence_target)}
        write_json(failed/"failure.json",record)
        runtime.record_definitive_failure(job_id,canonical(record))
        return {"job_id":job_id,"attempt_id":job["attempt_id"],"state":"failed_confirmed",
                "budget":runtime.status(job["run_id"])["authorization"],
                "next":runtime.next(job["run_id"]),"failure":record}
    finally:
        runtime.close()


def _output_path(state_root: Path, output: dict) -> Path:
    return confined(state_root.resolve(),output["local_path"])


def _single_output(runtime: Runtime, run_id: str, step_id: str) -> dict:
    receipt=runtime.step_output(run_id,step_id)
    outputs=receipt.get("outputs",[])
    if len(outputs)!=1:
        raise Conflict(f"{step_id} 必须有且只有一个输出。")
    return outputs[0]


def advance_local(state_root: Path, run_id: str) -> dict:
    runtime=Runtime(runtime_db(state_root),single_instance=True)
    try:
        advanced=[]
        while True:
            nxt=runtime.next(run_id)
            if nxt.get("state")!="pending":
                return {"advanced":advanced,"next":nxt,"status":runtime.status(run_id)}
            step_id=nxt["step"];kind=nxt["kind"]
            if kind=="image_generate":
                return {"advanced":advanced,"next":nxt,"status":runtime.status(run_id)}
            definition=next(step for step in runtime.plan(run_id)["steps"] if step["id"]==step_id)
            if step_id=="product-source" and kind=="compose" and definition.get("operation")=="import_product_source":
                reference=definition.get("reference_asset")
                if not isinstance(reference,dict):
                    raise Blocked("商品原图导入需要冻结的 reference_asset。")
                local_path=reference.get("local_path")
                expected_sha256=reference.get("sha256")
                asset_id=reference.get("asset_id")
                if not isinstance(local_path,str) or not local_path:
                    raise Blocked("商品原图 local_path 必须是 state_root 内相对路径。")
                if (not isinstance(expected_sha256,str) or len(expected_sha256)!=64 or
                        any(char not in "0123456789abcdef" for char in expected_sha256)):
                    raise Blocked("商品原图 SHA256 必须是 64 位小写十六进制。")
                try:
                    safe_id(asset_id)
                except FactoryError as exc:
                    raise Blocked("商品原图 asset_id 必须是非空安全 ID。") from exc
                state_root=state_root.resolve()
                source=confined(state_root,local_path)
                _,source_info=validate_product(source)
                if source_info["format"]!="PNG":
                    raise Blocked("商品原图必须是 PNG 格式。")
                if source_info["sha256"]!=expected_sha256:
                    raise Blocked("商品原图 SHA256 与冻结 reference_asset 不一致。")
                if run_id in {".",".."}:
                    raise Blocked("Runtime run_id 不能用于商品原图输出目录。")
                relative_output=f"runtime-runs/{run_id}/product_source_imported.png"
                target=confined(state_root,relative_output,must_exist=False)
                if target.is_symlink():
                    raise Blocked("商品原图导入目标不能是软链接。")
                if target.exists():
                    if not target.is_file() or file_hash(target)!=expected_sha256:
                        raise Conflict("商品原图导入目标已有不同文件，禁止覆盖。")
                else:
                    target.parent.mkdir(parents=True,exist_ok=True)
                    target=confined(state_root,relative_output,must_exist=False)
                    try:
                        with source.open("rb") as source_file, target.open("xb") as target_file:
                            shutil.copyfileobj(source_file,target_file)
                    except FileExistsError:
                        if (target.is_symlink() or not target.is_file() or
                                file_hash(target)!=expected_sha256):
                            raise Conflict("商品原图导入目标已有不同文件，禁止覆盖。")
                if file_hash(target)!=expected_sha256:
                    raise Blocked("商品原图导入期间文件内容发生变化。")
                output={"outputs":[{"name":target.name,"sha256":file_hash(target),
                                    "bytes":target.stat().st_size,
                                    "local_path":relative_output,"role":"product_rgba",
                                    "source_asset_id":asset_id}]}
                runtime.finish_local(run_id,step_id,output);advanced.append(step_id);continue
            if kind=="compose" and definition.get("operation")=="white_preview":
                source=_output_path(state_root,_single_output(runtime,run_id,"product-source"))
                target=state_root.resolve()/"runtime-runs"/run_id/"product_white_v1.png"
                result=derive_white_preview(source,target)
                result["output"]["local_path"]=str(target.relative_to(state_root.resolve()))
                runtime.finish_local(run_id,step_id,result);advanced.append(step_id);continue
            if kind=="compose" and definition.get("operation") in {
                    "compose_three_candidates_same_source", "compose_three_candidates_grounded_v2"}:
                source=_output_path(state_root,_single_output(runtime,run_id,"product-source"))
                backgrounds={scene:_output_path(state_root,_single_output(runtime,run_id,"background-"+scene))
                             for scene in ("outdoor","indoor","studio")}
                folder=state_root.resolve()/"runtime-runs"/run_id/"candidates"
                recipe=("grounded-v2" if definition["operation"]=="compose_three_candidates_grounded_v2"
                        else "v1")
                result=compose_candidates(source,backgrounds,folder,recipe=recipe)
                for item in result.values():
                    item["output"]["local_path"]=str(Path(item["output"]["path"]).relative_to(state_root.resolve()))
                runtime.finish_local(run_id,step_id,{"candidates":result});advanced.append(step_id);continue
            if kind=="human_review":
                runtime.wait_review(run_id,step_id);advanced.append("waiting:"+step_id)
                return {"advanced":advanced,"next":runtime.next(run_id),"status":runtime.status(run_id)}
            return {"advanced":advanced,"next":nxt,"blocked":"需要受信 live 适配器处理该节点。",
                    "status":runtime.status(run_id)}
    finally:
        runtime.close()
