"""Single-process V1 workflow loop with durable heartbeat and stop control."""
from __future__ import annotations

import csv
import io
import os
import time
from pathlib import Path

from .demo_live import create_demo_usage,demo_export,demo_import_metrics
from .runtime import Runtime
from .runtime_runner import advance_local,dispatch_next,runtime_db
from .util import FactoryError,atomic_write,now,process_lock,read_json,text,write_json
from .v1_live import project_status_outbox,publish_candidates,sync_review_to_runtime


def active_run(state_root:Path,run_id:str|None)->str:
    if run_id:return run_id
    path=state_root.resolve()/"active-v1-run.json"
    if not path.exists():raise FactoryError("尚无 active V1 run；先执行 runtime-init-demo。")
    value=read_json(path).get("run_id")
    if not isinstance(value,str):raise FactoryError("active-v1-run.json 无效。")
    return value


def _runner_dir(state_root:Path,run_id:str)->Path:
    path=state_root.resolve()/"runner"/run_id;path.mkdir(parents=True,exist_ok=True);return path


def runner_status(state_root:Path,run_id:str|None)->dict:
    run_id=active_run(state_root,run_id);runtime=Runtime(runtime_db(state_root))
    try:
        heartbeat=_runner_dir(state_root,run_id)/"heartbeat.json"
        jobs=[]
        for job in runtime.active_jobs(run_id):
            item=dict(job);item["job_file"]=str(_runner_job_path(state_root,job["job_id"]))
            jobs.append(item)
        return {"runtime":runtime.status(run_id),"active_jobs":jobs,
                "heartbeat":read_json(heartbeat) if heartbeat.exists() else None,
                "stop_requested":(_runner_dir(state_root,run_id)/"stop.requested").exists()}
    finally:runtime.close()


def _runner_job_path(state_root:Path,job_id:str)->Path:
    return state_root.resolve()/"runtime-jobs"/job_id/"job.json"


def request_stop(state_root:Path,run_id:str|None)->dict:
    run_id=active_run(state_root,run_id);path=_runner_dir(state_root,run_id)/"stop.requested"
    atomic_write(path,(now()+"\n").encode());return {"run_id":run_id,"stop_requested":True}


def _task_id(base,run_id):
    row=base.find_unique_typed("tasks","任务名",run_id,field_names=["任务名"])
    if not row:raise FactoryError("找不到 V1 Demo 任务。")
    return row["record_id"]


def _finish_export(base,state,state_root,run_id,runtime):
    review=runtime.step_output(run_id,"human-review")
    approved=[]
    latest={event["asset_id"]:event for event in review["events"]}
    approved=[asset_id for asset_id,event in latest.items() if event["decision"]=="approve"]
    if not approved:
        result={"status":"no_delivery","approved":[],"reason":"全部候选被人工驳回；不创建空 ZIP。"}
    else:
        trusted={event.get("review_record_id") for event in review["events"]}
        if None in trusted or len(trusted)!=len(review["events"]):
            raise FactoryError("Runtime 审核快照缺少 Engine 确认的审核记录。")
        result=demo_export(base,state,_task_id(base,run_id),allowed_review_record_ids=trusted)
    runtime.finish_local(run_id,"approved-export",result)
    return result


def _finish_metrics(base,state,state_root,run_id,runtime):
    export=runtime.step_output(run_id,"approved-export")
    if export.get("status")=="no_delivery":
        result={"status":"skipped_no_delivery","records":[]}
        runtime.finish_local(run_id,"demo-feedback",result);return result
    rows=[]
    for index,image in enumerate(export["manifest"]["images"],1):
        usage_id=f"{run_id}-USE-{index:03d}"
        create_demo_usage(base,state,_task_id(base,run_id),image["asset_record_id"],usage_id,
                          "DEMO","SIMULATED","Demo Review","SIM-"+str(index),
                          "2026-09-21T00:00:00+08:00")
        rows.append({"placement_id":usage_id,"date":"2026-09-21","timezone":"Asia/Shanghai",
                     "channel":"DEMO","impressions":str(1000+index*100),"clicks":str(30+index),
                     "orders":str(index),"metric_definition":"V1 Demo simulated orders_per_click",
                     "source_kind":"demo"})
    stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=["placement_id","date","timezone","channel","impressions","clicks","orders","metric_definition","source_kind"])
    writer.writeheader();writer.writerows(rows)
    path=state_root.resolve()/"runtime-runs"/run_id/"simulated-metrics.csv"
    atomic_write(path,stream.getvalue().encode())
    result=demo_import_metrics(base,state,_task_id(base,run_id),path,1)
    runtime.finish_local(run_id,"demo-feedback",result);return result


def _run_once(base,state,state_root:Path,run_id:str)->dict:
    run_id=active_run(state_root,run_id);actions=[]
    local=advance_local(state_root,run_id);actions.extend(local.get("advanced",[]))
    runtime=Runtime(runtime_db(state_root))
    try:
        nxt=runtime.next(run_id)
        if nxt.get("state")=="pending" and nxt.get("kind")=="image_generate":
            runtime.close();runtime=None
            job=dispatch_next(state_root,run_id);actions.append("dispatched:"+job["step"])
            result={"run_id":run_id,"actions":actions,"state":"waiting_worker","job":job}
        elif nxt.get("state")=="waiting_worker":
            jobs=runtime.active_jobs(run_id);result={"run_id":run_id,"actions":actions,"state":"waiting_worker","jobs":jobs}
        elif nxt.get("state")=="waiting_review":
            runtime.close();runtime=None
            published=publish_candidates(base,state,state_root,run_id)
            review=sync_review_to_runtime(base,state_root,run_id)
            actions.append("candidates_projected")
            result={"run_id":run_id,"actions":actions,"state":"waiting_review","publish":published,"review":review}
        elif nxt.get("state")=="pending" and nxt.get("kind")=="export":
            export=_finish_export(base,state,state_root,run_id,runtime);actions.append("export")
            result={"run_id":run_id,"actions":actions,"state":"delivery_complete","export":export}
        elif nxt.get("state")=="pending" and nxt.get("kind")=="metrics":
            metrics=_finish_metrics(base,state,state_root,run_id,runtime);actions.append("metrics")
            result={"run_id":run_id,"actions":actions,"state":"completed_demo","metrics":metrics}
        else:
            result={"run_id":run_id,"actions":actions,"state":nxt.get("state"),"next":nxt}
    finally:
        if runtime is not None:runtime.close()
    projection=project_status_outbox(base,state_root,run_id)
    result["projection"]=projection
    return result


def run_once(base,state,state_root:Path,run_id:str|None)->dict:
    run_id=active_run(state_root,run_id)
    with process_lock(_runner_dir(state_root,run_id)):
        return _run_once(base,state,state_root,run_id)


def run_loop(base,state,state_root:Path,run_id:str|None,interval:float=5.0,max_cycles:int=0)->dict:
    run_id=active_run(state_root,run_id)
    if interval<1 or interval>60:raise FactoryError("runner interval 必须在 1 至 60 秒。")
    directory=_runner_dir(state_root,run_id);stop=directory/"stop.requested"
    with process_lock(directory):
        if stop.exists():stop.unlink()
        cycle=0;last=None
        while True:
            cycle+=1
            heartbeat={"run_id":run_id,"pid":os.getpid(),"state":"running","cycle":cycle,
                       "level":"L1","headless_native_image":False,"updated_at":now(),"last_error":None}
            write_json(directory/"heartbeat.json",heartbeat)
            try:last=_run_once(base,state,state_root,run_id)
            except Exception as exc:
                heartbeat.update({"state":"blocked","updated_at":now(),"last_error":str(exc)[:500]})
                write_json(directory/"heartbeat.json",heartbeat);raise
            heartbeat.update({"state":last.get("state","unknown"),"updated_at":now(),"last_result":last})
            write_json(directory/"heartbeat.json",heartbeat)
            if last.get("state") in {"completed","completed_demo"}:
                return last
            if stop.exists():
                heartbeat.update({"state":"stopped","updated_at":now()});write_json(directory/"heartbeat.json",heartbeat)
                return {"run_id":run_id,"state":"stopped","cycle":cycle}
            if max_cycles and cycle>=max_cycles:return last
            time.sleep(interval)
