"""Controlled Feishu projection for the isolated V1-DEMO-KIDS namespace."""
from __future__ import annotations

import shutil
from pathlib import Path

from .image_pipeline import PRODUCT_SOURCE, background
from .runtime import Blocked, Runtime
from .runtime_runner import runtime_db
from .util import FactoryError, canonical, confined, file_hash, links, safe_id, text, write_json


NAMESPACE="V1-DEMO-KIDS"
POLICY="demo-v2"
_LINK_FIELDS={"流程","商品","选用提示词组件","来源任务","默认商品素材"}


def _find(base,table,field,value,fields=None):
    return base.find_unique_typed(table,field,value,field_names=fields or [field])


def _record(state,base,table,key_field,fields,effect_key):
    value=fields[key_field]
    row=state.effect(effect_key,lambda:_find(base,table,key_field,value,list(fields)),
                     lambda:base.create_record(table,fields))
    row=_find(base,table,key_field,value,list(fields))
    if row is None:
        raise FactoryError(f"{table}/{value} 写入后无法读回。")
    actual=row["fields"]
    for name,wanted in fields.items():
        got=actual.get(name)
        if name in _LINK_FIELDS:
            equal=links(got)==wanted
        elif isinstance(wanted,str):
            equal=text(got)==wanted
        elif isinstance(wanted,list) and all(isinstance(item,str) for item in wanted):
            equal=text(got)=="".join(wanted)
        elif isinstance(wanted,(int,float)) and not isinstance(wanted,bool) and isinstance(got,(int,float)) and not isinstance(got,bool):
            equal=float(got)==float(wanted)
        else:
            equal=got==wanted
        if not equal:
            raise FactoryError(f"{table}/{value} 已存在但字段 {name} 不一致，停止覆盖。")
    return row


def seed_demo(base,state,run_id:str)->dict:
    safe_id(run_id)
    if not run_id.startswith(NAMESPACE+"-"):
        raise FactoryError("V1 Demo run_id 必须使用 V1-DEMO-KIDS- 前缀。")
    sku=NAMESPACE+"-SKU-001"
    product_fields={"商品名":"V1 虚构白蓝童鞋","SKU":sku,"商品品牌":"无品牌虚构",
        "事实版本":"demo-v1","已确认事实":"虚构白色/浅蓝色儿童运动鞋，仅用于系统演示，不代表真实商品事实。",
        "禁止改变":"不得添加品牌、Logo、认证、功能、价格、尺码或健康宣称。",
        "事实来源":"本轮 Codex 原生图片工具生成的虚构演示素材；禁止真实发布。",
        "资料已确认":False,"示例数据":True}
    product=_record(state,base,"products","SKU",product_fields,f"v1-seed:product:{sku}")
    flow_name=NAMESPACE+" / Kids Background v1"
    flow_fields={"流程名":flow_name,"版本":"1.0","生产方式":["背景合成"],"已发布":True,
        "说明":"独立 Runtime：透明商品源→白底派生→3 张独立背景→3 候选→demo-v2 人审。",
        "输出宽度":1024,"输出高度":1024,"商品占画布比例":0.62,"示例数据":True}
    flow=_record(state,base,"workflows","流程名",flow_fields,f"v1-seed:workflow:{flow_name}")
    components=[
        ("product-source","商品透明源",PRODUCT_SOURCE),
        ("background-outdoor","户外场景",background("outdoor")["visual_prompt"]),
        ("background-indoor","室内场景",background("indoor")["visual_prompt"]),
        ("background-studio","棚拍场景",background("studio")["visual_prompt"]),
    ]
    component_ids=[]
    for order,(component_id,label,prompt) in enumerate(components,1):
        fields={"步骤名":f"{flow_name} / {label}","流程":[flow["record_id"]],"顺序":order,
                "节点类型":["提示词"],"提示词模板":prompt,"启用":True,
                "组件ID":component_id,"组件版本":"1.0"}
        row=_record(state,base,"steps","组件ID",fields,f"v1-seed:component:{component_id}")
        component_ids.append(row["record_id"])
    task_fields={"任务名":run_id,"商品":[product["record_id"]],"流程":[flow["record_id"]],
        "选用提示词组件":component_ids,"渠道":"DEMO","图片用途":"系统验收演示",
        "消费场景":"户外、室内、摄影棚三个独立候选","版位":"Demo 审图台",
        "视觉风格":"明亮、低饱和、真实商业摄影","附加要求":"禁止真实发布、投放或商业宣称。",
        "变体方向":"outdoor\nindoor\nstudio","数量":3,"最多调用次数":6,"提交":True,"取消":False,
        "示例数据":True,"系统状态":["待生图"],"运行ID":run_id,"命名空间":NAMESPACE,
        "模式":["demo"],"审核策略版本":POLICY,"运行版本":1}
    task=_record(state,base,"tasks","任务名",task_fields,f"v1-seed:task:{run_id}")
    return {"namespace":NAMESPACE,"product_record_id":product["record_id"],
            "workflow_record_id":flow["record_id"],"component_record_ids":component_ids,
            "task_record_id":task["record_id"],"run_id":run_id}


def _upload_checked(base,state,table,record,field,path:Path,download_root:Path):
    key=f"v1-upload:{table}:{record}:{field}:{file_hash(path)}"
    def find():
        items=base.get_record(table,record,[field])["fields"].get(field,[]) or []
        hits=[item for item in items if item.get("name")==path.name and item.get("size")==path.stat().st_size]
        if len(hits)>1:raise FactoryError("V1 附件出现重复绑定。")
        return hits[0] if hits else None
    result=state.effect(key,find,lambda:base.upload_attachment(table,record,field,path))
    remote=find() or result
    if not remote or not remote.get("file_token"):raise FactoryError("V1 附件上传后无法读回。")
    target=download_root/(table+"_"+record+"_"+path.name);target.parent.mkdir(parents=True,exist_ok=True)
    base.download_attachment(table,record,remote,target)
    if file_hash(target)!=file_hash(path):raise FactoryError("V1 附件往返 SHA256 不一致。")
    return remote


def _receipt_output(runtime,run_id,step_id):
    receipt=runtime.step_output(run_id,step_id);outputs=receipt.get("outputs",[])
    if len(outputs)!=1:raise FactoryError(f"{step_id} 输出数量不是 1。")
    return outputs[0]


def publish_candidates(base,state,state_root:Path,run_id:str)->dict:
    if not run_id.startswith(NAMESPACE+"-"):raise FactoryError("拒绝非 V1-DEMO-KIDS 运行。")
    task=_find(base,"tasks","任务名",run_id,["任务名","命名空间","模式","审核策略版本","运行ID","商品"])
    if not task:raise FactoryError("先执行 v1-demo-seed。")
    tf=task["fields"]
    if text(tf.get("命名空间"))!=NAMESPACE or text(tf.get("模式"))!="demo" or text(tf.get("审核策略版本"))!=POLICY or text(tf.get("运行ID"))!=run_id:
        raise FactoryError("V1 Demo 任务模式、命名空间、策略或运行ID不匹配。")
    product_ids=links(tf.get("商品"))
    if len(product_ids)!=1:raise FactoryError("V1 Demo 任务必须关联一个商品。")
    runtime=Runtime(runtime_db(state_root))
    try:
        nxt=runtime.next(run_id)
        if nxt.get("state")!="waiting_review":raise Blocked("Runtime 尚未进入 waiting_review。")
        source=_receipt_output(runtime,run_id,"product-source")
        white=runtime.step_output(run_id,"white-preview")["output"]
        backgrounds={scene:_receipt_output(runtime,run_id,"background-"+scene) for scene in ("outdoor","indoor","studio")}
        candidates=runtime.step_output(run_id,"candidate-composition")["candidates"]
    finally:runtime.close()
    roles=[("PRODUCT-RGBA","商品透明源","商品原图","product_rgba","",source),
           ("PRODUCT-WHITE","白底展示图","商品原图","product_white","",white)]
    for scene in ("outdoor","indoor","studio"):
        roles.append(("BG-"+scene.upper(),scene+" 背景","场景参考","background","",backgrounds[scene]))
    slots={"outdoor":"A","indoor":"B","studio":"C"}
    for scene in ("outdoor","indoor","studio"):
        roles.append(("CAND-"+scene.upper(),scene+" 候选","生成成图","candidate",slots[scene],candidates[scene]["output"]))
    created={};download_root=state_root/"runtime-runs"/run_id/"roundtrip"
    for suffix,label,asset_type,role,slot,output in roles:
        path=confined(state_root,output["local_path"])
        asset_id=run_id+"-"+suffix
        fields={"素材名":run_id+" / "+label,"资产ID":asset_id,"类型":[asset_type],
                "商品":product_ids,"来源任务":[task["record_id"]],"来源说明":"V1 Runtime 冻结链路，详见本地账本与回执。",
                "授权说明":"仅限 V1 虚构童鞋 Demo；禁止真实发布或投放。","允许用于生图":False,
                "SHA256":file_hash(path),"示例数据":True,"槽位":slot,"运行ID":run_id,
                "模式":["demo"],"资产角色":[role]}
        if role=="candidate":fields["审核状态"]=["待审核"]
        row=_record(state,base,"assets","资产ID",fields,f"v1-asset:{asset_id}")
        _upload_checked(base,state,"assets",row["record_id"],"图片",path,download_root)
        created[asset_id]=row["record_id"]
    source_id=created[run_id+"-PRODUCT-RGBA"]
    product=base.get_record("products",product_ids[0],["默认商品素材","商品照片"])
    if links(product["fields"].get("默认商品素材")) not in ([],[source_id]):
        raise FactoryError("Demo 商品已有不同默认商品素材，停止覆盖。")
    state.effect(f"v1-product-source:{run_id}",
        lambda:True if links(base.get_record("products",product_ids[0],["默认商品素材"])["fields"].get("默认商品素材"))==[source_id] else None,
        lambda:base.update_record("products",product_ids[0],{"默认商品素材":[source_id]}))
    white_path=confined(state_root,white["local_path"])
    _upload_checked(base,state,"products",product_ids[0],"商品照片",white_path,download_root)
    state.effect(f"v1-task-wait-review:{run_id}",
        lambda:True if text(base.get_record("tasks",task["record_id"],["系统状态"])["fields"].get("系统状态"))=="待审核" else None,
        lambda:base.update_record("tasks",task["record_id"],{"系统状态":["待审核"],"错误摘要":""}))
    return {"run_id":run_id,"task_record_id":task["record_id"],"assets":created,
            "candidate_asset_ids":[run_id+"-CAND-"+scene.upper() for scene in ("outdoor","indoor","studio")],
            "status":"waiting_human_review","roundtrip_verified":len(created)}


def sync_review_to_runtime(base,state_root:Path,run_id:str)->dict:
    """Read trusted Feishu-created review rows; never creates or edits reviews."""
    from .demo_live import _candidate_rows,_demo_task_base,_latest_review
    task,product,product_id=_demo_task_base(base,_find(base,"tasks","任务名",run_id,["任务名"])["record_id"])
    candidates=_candidate_rows(base,task,product_id)
    snapshot={"run_id":run_id,"mode":"demo","candidates":[],"events":[]}
    missing=[]
    runtime=Runtime(runtime_db(state_root))
    try:
        confirmed=runtime.confirmed_review_records(run_id)
        allowed=set(confirmed)
        for row in candidates:
            fields=row["fields"];asset_id=text(fields.get("资产ID"));sha=text(fields.get("SHA256"))
            snapshot["candidates"].append({"asset_id":asset_id,"sha256":sha})
            try:choice,review=_latest_review(base,row["record_id"],allowed_record_ids=allowed)
            except FactoryError as exc:
                if "缺少人工审核记录" in str(exc):missing.append(asset_id);continue
                raise
            rf=review["fields"]
            receipt=confirmed.get(review["record_id"],{})
            expected_decision="approved" if choice=="通过" else ("rejected" if choice=="驳回" else "revoked")
            if (receipt.get("asset_id")!=asset_id or receipt.get("sha256")!=sha
                    or receipt.get("decision")!=expected_decision
                    or receipt.get("reviewer_id") not in links(rf.get("审核人"))):
                raise FactoryError("Runner 拒绝未经 Engine write journal 确认的审核。")
            if text(rf.get("审核策略版本"))!=POLICY or text(rf.get("运行ID"))!=run_id:
                raise FactoryError("V1 审核策略或运行ID不匹配。")
            reviewers=links(rf.get("审核人"))
            revision=rf.get("审核版本")
            if isinstance(revision,bool) or not isinstance(revision,(int,float)) or int(revision)!=revision:
                raise FactoryError("V1 审核缺少整数审核版本。")
            snapshot["events"].append({"asset_id":asset_id,"revision":int(revision),"run_id":run_id,
                "mode":"demo","sha256":sha,"decision":"approve" if choice=="通过" else ("reject" if choice=="驳回" else "revoked"),
                "actor_id":reviewers[0] if len(reviewers)==1 else "","human_confirmed":rf.get("人工确认") is True,
                "reason":text(rf.get("原因")) or "人工审核已记录",
                "demo_visual_ok":rf.get("Demo视觉检查") is True,"demo_use_only":rf.get("仅限演示使用") is True,
                "review_record_id":review["record_id"]})
        if missing:return {"status":"waiting","missing":missing}
        runtime.finish_review(run_id,"human-review",validated_snapshot=snapshot)
        return {"status":"accepted","snapshot":snapshot,"next":runtime.next(run_id)}
    finally:runtime.close()


_STATUS_MAP={"queued":"待生图","running":"生成中","waiting_worker":"生成中",
             "waiting_review":"待审核","delivery_pending_sync":"待审核",
             "blocked":"阻塞","completed":"已完成"}


def project_status_outbox(base,state_root:Path,run_id:str,limit:int=100)->dict:
    """Replay only status projections; it cannot dispatch or regenerate images."""
    task=_find(base,"tasks","任务名",run_id,["任务名","运行ID","命名空间","运行版本","系统状态"])
    if not task or text(task["fields"].get("命名空间"))!=NAMESPACE:
        raise FactoryError("找不到目标 V1-DEMO-KIDS 任务。")
    runtime=Runtime(runtime_db(state_root))
    done=[]
    try:
        for _ in range(limit):
            op=runtime.claim_outbox("v1-status-projector")
            if op is None:break
            if op.get("run_id")!=run_id or op.get("kind")!="status_projection":
                runtime.unknown_outbox(op["op_id"],"projector scope mismatch")
                raise FactoryError("outbox 含不属于当前运行的操作，停止投影。")
            payload=op["payload"];version=payload.get("event_seq")
            if not isinstance(version,int):
                runtime.unknown_outbox(op["op_id"],"missing event_seq")
                raise FactoryError("状态投影缺少 event_seq。")
            fields={"运行ID":run_id,"运行版本":version,
                    "系统状态":[_STATUS_MAP.get(payload.get("state"),"阻塞")],"错误摘要":""}
            try:
                current=base.get_record("tasks",task["record_id"],["运行ID","运行版本","系统状态"])["fields"]
                current_version=current.get("运行版本",0)
                if isinstance(current_version,bool) or not isinstance(current_version,(int,float)):
                    raise FactoryError("任务运行版本不是数字，停止状态投影。")
                if int(current_version)>version:
                    runtime.confirm_outbox(op["op_id"],expected_hash=op["payload_hash"],observed_hash=op["payload_hash"],
                                           receipt={"task_record_id":task["record_id"],"superseded_by":int(current_version)})
                    done.append(op["op_id"]);continue
                if int(current_version)<version or text(current.get("系统状态"))!=fields["系统状态"][0]:
                    base.update_record("tasks",task["record_id"],fields)
                read=base.get_record("tasks",task["record_id"],["运行ID","运行版本","系统状态"])["fields"]
                if text(read.get("运行ID"))!=run_id or int(read.get("运行版本",-1))!=version or text(read.get("系统状态"))!=fields["系统状态"][0]:
                    raise FactoryError("状态投影写后读回不一致。")
                runtime.confirm_outbox(op["op_id"],expected_hash=op["payload_hash"],observed_hash=op["payload_hash"],
                                       receipt={"task_record_id":task["record_id"],"run_revision":version})
                done.append(op["op_id"])
            except Exception as exc:
                try:runtime.unknown_outbox(op["op_id"],type(exc).__name__)
                except Blocked:pass
                raise
        return {"confirmed":done,"remaining":len(runtime.pending_outbox())}
    finally:runtime.close()
