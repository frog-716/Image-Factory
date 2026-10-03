from __future__ import annotations
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from .util import FactoryError, UnknownWrite, canonical, confined, file_hash, now, process_lock, read_json, safe_id, text, write_json
from .lark import LarkCLI,FeishuGateway
from .state import State
from .engine import Engine,restore_archive
from .sync import bootstrap,schema_check,schema_plan,apply_schema_additions,setup_views,seed_workflows,import_metrics,metric_summary,probe_feishu,probe_attachments
from .demo_live import demo_export,create_demo_usage,demo_import_metrics,demo_metric_summary

ROOT=Path(__file__).resolve().parents[1]
DEFAULT={"base_token":"","lark_cli":"","identity":"user","tables":{},"max_images_per_task":12,
         "max_calls_per_task":18,"max_image_bytes":20*1024*1024,"max_archive_bytes":128*1024*1024,
         "cli_timeout_seconds":120,"attachment_timeout_seconds":300,
         "attachment_readback_delays_seconds":[0,1,2,4],"reviewer_open_ids":[]}

def configuration(path):
    return {**DEFAULT,**(read_json(path) if path.exists() else {})}

def parser():
    p=argparse.ArgumentParser(description="飞书优先的电商图片工作流。默认不生图、不调用付费 API。")
    p.add_argument("--config",default="config.local.json");p.add_argument("--state",default="var/live")
    sub=p.add_subparsers(dest="command",required=True)
    s=sub.add_parser("demo",help="离线全流程演练，完全不联网")
    s.add_argument("--out",default="var/demo")
    s=sub.add_parser("runtime-init-demo",help="在同一账本初始化 V1-DEMO-KIDS Workflow 与项目级 6 次授权")
    s.add_argument("--run",required=True);s.add_argument("--authorization-id",default="V1-DEMO-KIDS-20260921")
    s.add_argument("--actor-id",default="user_launch_message")
    s.add_argument("--template",default=str(ROOT/"templates/runtime-workflow-kids-demo-v1.json"))
    s=sub.add_parser("runtime-init-asset-batch",help="初始化独立虚构童鞋多角度素材批次；本地 6 次上限，不触碰现役 Demo")
    s.add_argument("--run",required=True);s.add_argument("--authorization-id",required=True)
    s.add_argument("--actor-id",required=True);s.add_argument("--reference-image",required=True)
    s.add_argument("--template",default=str(ROOT/"templates/runtime-fictional-kids-assets-v1.json"))
    s=sub.add_parser("runtime-init-topstar-source",help="冻结 TOPSTAR 5056 透明商品源图任务与共享 6 次授权；不派发图片")
    s.add_argument("--run",required=True);s.add_argument("--authorization-id",required=True)
    s.add_argument("--actor-id",required=True);s.add_argument("--reference-image",required=True)
    s=sub.add_parser("runtime-status",help="读取独立 Runtime 状态")
    s.add_argument("--run",required=True)
    s=sub.add_parser("runtime-advance",help="持续推进确定性本地节点，遇图片/审核/live 节点即停")
    s.add_argument("--run",required=True)
    s=sub.add_parser("runtime-dispatch-next",help="原子预留预算并派发下一个原生图片 Job")
    s.add_argument("--run",required=True)
    s=sub.add_parser("runtime-job",help="重新物化既有 Job，不新建 Attempt、不重复预留")
    s.add_argument("--job",required=True)
    s=sub.add_parser("runtime-stage-image",help="登记一次真实原生图片结果并验收文件")
    s.add_argument("--job",required=True);s.add_argument("--image",required=True);s.add_argument("--evidence",required=True)
    s.add_argument("--actual-model",required=True);s.add_argument("--call-id",default="not_reported")
    s=sub.add_parser("runtime-reject-image",help="保存明确返回但契约不合格的图片；本次预算不退款")
    s.add_argument("--job",required=True);s.add_argument("--image",required=True);s.add_argument("--evidence",required=True)
    s.add_argument("--reason",required=True)
    s=sub.add_parser("runner-once",help="独立流程引擎推进一个周期")
    s.add_argument("--run")
    s=sub.add_parser("runner-loop",help="前台单进程循环；遇原生生图停在 waiting_worker")
    s.add_argument("--run");s.add_argument("--interval",type=float,default=5.0);s.add_argument("--max-cycles",type=int,default=0)
    s=sub.add_parser("runner-status",help="只读流程引擎心跳、Job 和预算状态")
    s.add_argument("--run")
    s=sub.add_parser("product-v1-status",help="只读输出 Product V1 人类操作阶段；不推进 Runtime")
    s.add_argument("--run")
    s=sub.add_parser("human-ui",help="启动仅限本机的两页 Human UI；审核只经 Engine API")
    s.add_argument("--run");s.add_argument("--port",type=int,default=8765);s.add_argument("--open",action="store_true")
    s=sub.add_parser("runner-stop",help="请求流程引擎在当前周期后停止")
    s.add_argument("--run")
    sub.add_parser("intake-once",help="受信飞书表单受理一个周期；仅排队，不生图")
    s=sub.add_parser("intake-loop",help="受信飞书表单持续受理；仅排队，不生图")
    s.add_argument("--interval",type=float,default=5.0)
    s.add_argument("--max-cycles",type=int,default=0)
    s=sub.add_parser("bind",help="保存已明确选择的空白 Base token，不访问远端")
    s.add_argument("--base-token",required=True);s.add_argument("--cli");s.add_argument("--reviewer",action="append",default=[])
    sub.add_parser("doctor",help="检查本机、CLI 命令和 Base 只读权限")
    s=sub.add_parser("bootstrap");s.add_argument("--apply",action="store_true")
    sub.add_parser("schema-plan",help="只读列出当前 Base 相对代码契约需要新增的字段及漂移")
    s=sub.add_parser("migrate-v1",help="仅新增 schema-plan 列出的 V1 字段；不建表、不删表、不改记录")
    s.add_argument("--apply",action="store_true")
    s=sub.add_parser("migrate-v1-reconcile",help="仅协调已确认未落地的 schema invalid-parameter unknown")
    s.add_argument("--field",required=True);s.add_argument("--error-code",required=True)
    sub.add_parser("schema-check");sub.add_parser("setup-views")
    s=sub.add_parser("seed-workflows");s.add_argument("--template",default=str(ROOT/"templates/workflow-background.json"))
    sub.add_parser("queue");sub.add_parser("status")
    sub.add_parser("probe-feishu");sub.add_parser("probe-attachments")
    s=sub.add_parser("prepare");s.add_argument("--task",required=True)
    for cmd in ("start-slot","retry-slot","receive","stage-result"):
        s=sub.add_parser(cmd);s.add_argument("--run",required=True);s.add_argument("--slot",required=True)
        if cmd=="retry-slot":
            s.add_argument("--reason",required=True);s.add_argument("--ack-possible-charge",action="store_true")
        if cmd=="receive":
            s.add_argument("--receipt",required=True)
        if cmd=="stage-result":
            s.add_argument("--image",required=True);s.add_argument("--evidence",required=True)
            s.add_argument("--actual-model",required=True);s.add_argument("--used-asset",action="append",default=[])
            s.add_argument("--call-id",default="not_reported")
    for cmd in ("push","sync-reviews","export","archive","cancel"):
        s=sub.add_parser(cmd);s.add_argument("--run",required=True)
        if cmd=="export":s.add_argument("--allow-partial",action="store_true")
    s=sub.add_parser("record-capability");s.add_argument("--image",required=True);s.add_argument("--evidence",required=True)
    s.add_argument("--actual-model",required=True)
    s=sub.add_parser("restore");s.add_argument("--archive",required=True)
    s=sub.add_parser("resolve-effect");s.add_argument("--key",required=True);s.add_argument("--confirmed-absent",action="store_true")
    s=sub.add_parser("import-metrics");s.add_argument("--csv",required=True);s.add_argument("--revision",type=int,default=1)
    s.add_argument("--confirm-correction",action="store_true")
    sub.add_parser("metrics-summary")
    s=sub.add_parser("demo-export",help="导出已人工审核通过的预生成 Demo 素材，不启动生产引擎")
    s.add_argument("--task",required=True)
    s=sub.add_parser("v1-demo-seed",help="仅在 V1-DEMO-KIDS 命名空间创建虚构童鞋/组件/任务")
    s.add_argument("--run",required=True)
    s=sub.add_parser("v1-demo-publish",help="把 waiting_review 的 V1 Runtime 资产经 Gateway 回写并往返校验")
    s.add_argument("--run",required=True)
    s=sub.add_parser("demo-use",help="创建明确标记的 Demo 模拟使用记录")
    s.add_argument("--task",required=True);s.add_argument("--asset",required=True);s.add_argument("--usage-id",required=True)
    s.add_argument("--channel",required=True);s.add_argument("--account",required=True);s.add_argument("--placement",required=True)
    s.add_argument("--page-id",required=True);s.add_argument("--start-time",required=True)
    s=sub.add_parser("demo-import-metrics",help="导入只关联 Demo 使用记录的模拟指标")
    s.add_argument("--task",required=True);s.add_argument("--csv",required=True);s.add_argument("--revision",type=int,default=1)
    s=sub.add_parser("demo-metrics-summary",help="查询指定 Demo 任务的模拟指标")
    s.add_argument("--task",required=True)
    return p

def compact_run(run):
    return {"run_id":run["id"],"task_id":run["task_id"],"slots":{k:v["state"] for k,v in run["slots"].items()},
            "calls_reserved":run["calls_reserved"],"call_limit":run["source"]["max_calls"],"cancelled":run["cancelled"]}

def stage(engine,run_id,sid,image,evidence,model,used_assets,call_id):
    from .media import inspect_image
    run=engine.state.run(run_id);slot=engine._slot(run,sid)
    if slot["state"]!="STARTED":raise FactoryError("该槽位尚未派发或已经完成。")
    image=Path(image);evidence=Path(evidence)
    inspect_image(image)
    if not evidence.is_file() or evidence.is_symlink() or not 0<evidence.stat().st_size<=200000:
        raise FactoryError("工具证据必须是明确选择的普通文本文件。")
    req=engine.request(run,sid);available={x["record_id"]:x for x in run["inputs"]}
    if len(set(used_assets))!=len(used_assets) or any(x not in available for x in used_assets):
        raise FactoryError("实际引用素材 ID 无效或重复。")
    attempt=slot["attempts"][-1]["id"];root=engine.run_root(run_id)
    incoming=root/"incoming";incoming.mkdir(parents=True,exist_ok=True)
    target=incoming/(sid+"_"+attempt+image.suffix.lower())
    target_evidence=incoming/(sid+"_"+attempt+".txt")
    if image.resolve()!=target.resolve():shutil.copyfile(image,target)
    if evidence.resolve()!=target_evidence.resolve():shutil.copyfile(evidence,target_evidence)
    receipt={"run_id":run_id,"slot":sid,"attempt_id":attempt,"producer":"codex_native",
        "actual_model":model,"provider_call_id":call_id,"generated_at":now(),"prompt_sha256":req["prompt_sha256"],
        "input_sha256":[available[x]["sha256"] for x in used_assets],"image_path":str(target.relative_to(root)),
        "tool_evidence_path":str(target_evidence.relative_to(root))}
    destination=incoming/(sid+"_"+attempt+".json");write_json(destination,receipt)
    return engine.receive(run_id,sid,destination)

def execute(args):
    cfg_path=Path(args.config).resolve();config=configuration(cfg_path);state_root=Path(args.state).resolve()
    if args.command=="demo":
        from .demo import run_demo
        with process_lock(Path(args.out).parent/".demo-lock"):
            return run_demo(Path(args.out),ROOT)
    if args.command=="bind":
        token=safe_id(args.base_token)
        if config.get("base_token") and config["base_token"]!=token:
            raise FactoryError("配置已绑定其他 Base。请新建配置和独立 --state，禁止混用执行账本。")
        config["base_token"]=token
        if args.cli:config["lark_cli"]=args.cli
        if args.reviewer:config["reviewer_open_ids"]=[safe_id(x) for x in args.reviewer]
        write_json(cfg_path,config)
        return {"saved":str(cfg_path),"remote_changed":False}
    if args.command in ("intake-once","intake-loop"):
        if not isinstance(config.get("intake_grant"),dict):
            raise FactoryError("缺少本机受信授权；不会读取或受理飞书表单。")
        if not isinstance(config.get("intake_profile"),dict):
            raise FactoryError("缺少本机受信任务配置；不会读取或受理飞书表单。")
        if args.command=="intake-loop" and (
            args.interval<=0 or args.max_cycles<0
        ):
            raise FactoryError("接单循环的 interval 必须大于 0，max-cycles 不能为负数。")
        from .form_intake_service import poll_form_once
        cycles=0
        while True:
            if args.command=="intake-loop" and configuration(cfg_path)!=config:
                raise FactoryError("本机受信配置或授权已变化；接单循环停止，请重新启动。")
            with process_lock(state_root):
                state=State(state_root)
                try:
                    if config.get("base_token"):state.bind_base(config["base_token"])
                    engine=Engine(FeishuGateway(config),state,config)
                    result=poll_form_once(engine,config["intake_grant"])
                finally:state.close()
            cycles+=1
            if args.command=="intake-once":return result
            if args.max_cycles and cycles>=args.max_cycles:
                return {**result,"cycles":cycles}
            time.sleep(args.interval)
    if args.command=="product-v1-status":
        from .human_ops import describe_run
        from .runtime import Runtime
        from .runtime_runner import runtime_db
        from .runner_service import active_run
        if args.run is None and not (state_root/"active-v1-run.json").exists():
            return {"run_id":None,"phase":"no_active_run","allowed_actions":[],
                    "next_instruction":"暂无当前任务。待商品资料与本机受理授权确认后，再通过飞书表单创建任务。",
                    "read_only":True}
        run_id=active_run(state_root,args.run)
        runtime=Runtime(runtime_db(state_root))
        try:return describe_run(runtime.status(run_id)).as_dict()
        finally:runtime.close()
    if args.command=="human-ui":
        from .human_ui_server import serve_human_ui
        if not 1<=args.port<=65535:raise FactoryError("Human UI port 必须在 1 至 65535。")
        return serve_human_ui(FeishuGateway(config),state_root,args.run,args.port,args.open)
    if args.command in ("runner-status","runner-stop"):
        from .runner_service import request_stop,runner_status
        if args.command=="runner-status":return runner_status(state_root,args.run)
        return request_stop(state_root,args.run)
    if args.command=="runner-loop":
        from .runner_service import run_loop
        state=State(state_root)
        try:
            if config.get("base_token"):state.bind_base(config["base_token"])
            return run_loop(FeishuGateway(config),state,state_root,args.run,args.interval,args.max_cycles)
        finally:state.close()
    if args.command.startswith("runtime-"):
        from .runtime import Runtime
        from .runtime_runner import (advance_local,create_asset_batch,create_demo,dispatch_next,materialize_job,
                                     create_topstar_source_batch,reject_image,runtime_db,stage_image)
        with process_lock(state_root):
            if args.command=="runtime-init-demo":
                return create_demo(state_root,Path(args.template),args.run,args.authorization_id,args.actor_id)
            if args.command=="runtime-init-asset-batch":
                return create_asset_batch(state_root,Path(args.template),args.run,
                                          args.authorization_id,args.actor_id,Path(args.reference_image))
            if args.command=="runtime-init-topstar-source":
                return create_topstar_source_batch(state_root,args.run,args.authorization_id,
                                                   args.actor_id,Path(args.reference_image))
            if args.command=="runtime-advance":return advance_local(state_root,args.run)
            if args.command=="runtime-dispatch-next":return dispatch_next(state_root,args.run)
            if args.command=="runtime-stage-image":
                return stage_image(state_root,args.job,Path(args.image),Path(args.evidence),args.actual_model,args.call_id)
            if args.command=="runtime-reject-image":
                return reject_image(state_root,args.job,Path(args.image),Path(args.evidence),args.reason)
            runtime=Runtime(runtime_db(state_root),single_instance=True)
            try:
                if args.command=="runtime-status":return runtime.status(args.run)
                if args.command=="runtime-job":return {"job_file":str(materialize_job(state_root,runtime,args.job))}
            finally:
                runtime.close()
    if args.command=="doctor":
        from PIL import __version__ as pillow_version
        cli=LarkCLI(config)
        version_lines=cli.call(["--version"],help_only=True).strip().splitlines()
        version=version_lines[0][:100] if version_lines else "not_reported"
        for cmd in ("+record-upload-attachment","+record-download-attachment","+view-set-card","+view-set-visible-fields","+view-set-group","+view-set-sort"):
            cli.call(["base",cmd,"--help"],help_only=True)
        report={"python":sys.version.split()[0],"pillow":pillow_version,"larkcli":version,
                "commands_present":True,"native_image_verified":(state_root/"capability.json").exists()}
        if config.get("base_token"):
            cli.call(["base","+base-get","--base-token",config["base_token"]])
            report["base_read_verified"]=True
        else:report["base_read_verified"]=False
        return report
    with process_lock(state_root):
        state=State(state_root)
        try:
            if config.get("base_token"):
                state.bind_base(config["base_token"])
            if args.command=="status":
                return {"runs":[compact_run(r) for r in state.list_runs()],"unknown_remote_writes":state.pending_effects()}
            if args.command=="resolve-effect":
                state.resolve_effect(args.key,args.confirmed_absent);return {"retry_authorized":args.key}
            if args.command=="record-capability":
                from .media import inspect_image
                image=Path(args.image);info=inspect_image(image);evidence=Path(args.evidence)
                if not evidence.is_file() or evidence.is_symlink() or not 0<evidence.stat().st_size<=200000:
                    raise FactoryError("探针缺少有效工具调用摘要。")
                cap={"producer":"codex_native","verified":True,"actual_model":args.actual_model,
                     "checked_at":now(),"image_sha256":info["sha256"],"evidence_sha256":file_hash(evidence),
                     "basis":"local artifact check and operator-reported tool evidence, not cryptographic provider attestation"}
                write_json(state_root/"capability.json",cap)
                (state_root/"probe").mkdir(exist_ok=True)
                shutil.copyfile(image,state_root/"probe"/("probe"+image.suffix.lower()))
                shutil.copyfile(evidence,state_root/"probe"/"tool-evidence.txt")
                return cap
            base=FeishuGateway(config)
            if args.command=="v1-demo-seed":
                from .v1_live import seed_demo
                return seed_demo(base,state,args.run)
            if args.command=="v1-demo-publish":
                from .v1_live import publish_candidates
                return publish_candidates(base,state,state_root,args.run)
            if args.command=="runner-once":
                from .runner_service import run_once
                return run_once(base,state,state_root,args.run)
            if args.command=="demo-export":return demo_export(base,state,args.task)
            if args.command=="demo-use":
                asset=args.asset
                try:
                    asset_row=base.get_record("assets",safe_id(asset))
                except FactoryError:
                    asset_row=base.find_unique("assets","资产ID",asset)
                if not asset_row:
                    raise FactoryError("找不到 Demo 资产记录或资产 ID。")
                asset_id=asset_row["record_id"]
                return create_demo_usage(base,state,args.task,asset_id,args.usage_id,args.channel,args.account,args.placement,args.page_id,args.start_time)
            if args.command=="demo-import-metrics":return demo_import_metrics(base,state,args.task,Path(args.csv),args.revision)
            if args.command=="demo-metrics-summary":return demo_metric_summary(base,args.task)
            engine=Engine(base,state,config)
            if args.command=="bootstrap":return bootstrap(base,state,cfg_path,args.apply)
            if args.command=="schema-plan":return schema_plan(base)
            if args.command=="migrate-v1":return apply_schema_additions(base,state) if args.apply else schema_plan(base)
            if args.command=="migrate-v1-reconcile":
                from .sync import reconcile_schema_invalid_parameter
                return reconcile_schema_invalid_parameter(base,state,args.field,args.error_code)
            if args.command=="schema-check":return schema_check(base)
            if args.command=="probe-feishu":return probe_feishu(base,state)
            if args.command=="probe-attachments":return probe_attachments(base,state)
            if args.command=="setup-views":return setup_views(base,state)
            if args.command=="seed-workflows":return seed_workflows(base,state,Path(args.template))
            if args.command=="queue":
                return [{"task_id":r["record_id"],"name":r["fields"].get("任务名"),"status":r["fields"].get("系统状态"),
                         "count":r["fields"].get("数量")} for r in base.list_records("tasks")
                        if r["fields"].get("提交") is True and r["fields"].get("取消") is not True]
            if args.command=="prepare":return compact_run(engine.prepare(args.task))
            if args.command=="start-slot":
                result=engine.start(args.run,args.slot)
                return {"run_id":args.run,"slot":args.slot,"attempt_id":result["attempt_id"],
                    "dispatch_file":str(engine.run_root(args.run)/"dispatch"/(args.slot+"_"+result["attempt_id"]+".json")),
                    "next":"Codex reads the dispatch file and invokes its verified native image tool once."}
            if args.command=="retry-slot":return engine.retry_slot(args.run,args.slot,args.reason,args.ack_possible_charge)
            if args.command=="receive":
                s=engine.receive(args.run,args.slot,Path(args.receipt));return {"slot":args.slot,"state":s["state"],"sha256":s["final_sha256"]}
            if args.command=="stage-result":
                s=stage(engine,args.run,args.slot,args.image,args.evidence,args.actual_model,args.used_asset,args.call_id)
                return {"slot":args.slot,"state":s["state"],"sha256":s["final_sha256"]}
            if args.command=="push":return compact_run(engine.push(args.run))
            if args.command=="sync-reviews":
                return {s:d["decision"] for s,d in engine.sync_reviews(args.run).items()}
            if args.command=="export":
                path,manifest=engine.export(args.run,args.allow_partial);return {"zip":str(path),"manifest":manifest}
            if args.command=="archive":return {"archive":str(engine.archive(args.run))}
            if args.command=="cancel":return compact_run(engine.cancel(args.run))
            if args.command=="restore":
                import zipfile
                path=Path(args.archive)
                with zipfile.ZipFile(path) as z:
                    info=z.getinfo("run.json")
                    if info.file_size>32*1024*1024:raise FactoryError("备份运行记录过大。")
                    header=json.loads(z.read("run.json"))
                remote=base.get_record("tasks",safe_id(header.get("task_id","")))["fields"]
                if text(remote.get("运行ID"))!=header.get("id") or text(remote.get("最新档案SHA256"))!=file_hash(path):
                    raise FactoryError("恢复包与飞书最新检查点不一致。先人工核对最新档案，禁止使用旧包恢复并重画。")
                return compact_run(restore_archive(path,state,base.token))
            if args.command=="import-metrics":return import_metrics(base,state,Path(args.csv),args.revision,args.confirm_correction)
            if args.command=="metrics-summary":return metric_summary(base,include_demo=False)
            raise FactoryError("未知命令。")
        finally:
            state.close()

def main():
    args=parser().parse_args()
    try:
        result=execute(args)
        print(json.dumps({"ok":True,"result":result},ensure_ascii=False,indent=2,allow_nan=False))
        return 0
    except (FactoryError,ValueError,KeyError,FileNotFoundError,PermissionError) as exc:
        code=3 if isinstance(exc,UnknownWrite) else 2
        print(json.dumps({"ok":False,"error":str(exc),"needs_reconciliation":code==3},ensure_ascii=False),file=sys.stderr)
        return code
