"""Local V1 runner adapter.

The adapter advances deterministic local nodes and stages real native-image
files.  It never invokes an image provider, approves an image, writes Feishu,
or falls back to an API.  A successful dispatch is the atomic budget boundary;
the caller then performs exactly one native image-tool call for that job.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .image_pipeline import compose_candidates, derive_white_preview, validate_product
from .media import inspect_image
from .runtime import Blocked, Conflict, Runtime, canonical, digest
from .util import atomic_write, confined, file_hash, now, read_json, safe_id, write_json


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
            if kind=="compose" and definition.get("operation")=="white_preview":
                source=_output_path(state_root,_single_output(runtime,run_id,"product-source"))
                target=state_root.resolve()/"runtime-runs"/run_id/"product_white_v1.png"
                result=derive_white_preview(source,target)
                result["output"]["local_path"]=str(target.relative_to(state_root.resolve()))
                runtime.finish_local(run_id,step_id,result);advanced.append(step_id);continue
            if kind=="compose" and definition.get("operation")=="compose_three_candidates_same_source":
                source=_output_path(state_root,_single_output(runtime,run_id,"product-source"))
                backgrounds={scene:_output_path(state_root,_single_output(runtime,run_id,"background-"+scene))
                             for scene in ("outdoor","indoor","studio")}
                folder=state_root.resolve()/"runtime-runs"/run_id/"candidates"
                result=compose_candidates(source,backgrounds,folder)
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
