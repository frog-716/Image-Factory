from __future__ import annotations
import copy
import io
import json
import shutil
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from .media import inspect_image, compose
from .model import sources, compile_prompt
from .state import State
from .util import (FactoryError, canonical, confined, digest, file_hash, integer,
                   links, now, read_json, safe_id, text, write_json, atomic_write)


def deterministic_zip(target: Path, entries: dict[str,bytes]):
    target.parent.mkdir(parents=True,exist_ok=True)
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,"w",compression=zipfile.ZIP_DEFLATED) as archive:
        for name,content in sorted(entries.items()):
            if Path(name).is_absolute() or ".." in Path(name).parts or "\\" in name:
                raise FactoryError("归档文件名不安全。")
            info=zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=0o100644<<16
            archive.writestr(info,content)
    atomic_write(target,buffer.getvalue())

class Engine:
    def __init__(self,base,state:State,config:dict):
        self.base,self.state,self.config=base,state,config
        self.allow_demo=bool(base.is_mock)
        self.state.bind_base(base.token)

    def current_source(self,task_id):
        return sources(self.base,task_id,self.config.get("max_images_per_task",12),
                       self.config.get("max_calls_per_task",18),self.allow_demo)

    def run_root(self,run_id):
        return self.state.root/"runs"/safe_id(run_id)

    def guard(self,run):
        if run.get("cancelled"):
            raise FactoryError("本地任务已取消。")
        current=self.current_source(run["task_id"])
        if digest(current)!=run["source_digest"]:
            raise FactoryError("任务、商品、素材或流程在提交后发生改变。冻结旧任务，复制为新任务后再生成。")
        for item in run["inputs"]:
            p=confined(self.run_root(run["id"]),item["path"])
            if file_hash(p)!=item["sha256"]:
                raise FactoryError("本地输入素材校验失败，禁止继续。")

    def _status(self,run,status,message=""):
        self.base.update_record("tasks",run["task_id"],{"运行ID":run["id"],"系统状态":status,"错误摘要":message[:500]})

    def prepare(self,task_id):
        safe_id(task_id)
        current=self.current_source(task_id)
        old=self.state.by_task(task_id)
        if old:
            self.guard(old)
            self.archive(old["id"])
            return old
        if text(self.base.get_record("tasks",task_id)["fields"].get("运行ID")):
            raise FactoryError("飞书已有运行ID，但本机缺少执行账本。先恢复最新运行档案，禁止重新派发。")
        rid="run_"+digest({"base":self.base.token,"task":task_id})[:20]
        root=self.run_root(rid);root.mkdir(parents=True,exist_ok=True)
        inputs=[]
        for item in current["assets"]:
            temporary=root/"inputs"/(safe_id(item["record_id"])+".download")
            self.base.download_attachment("assets",item["record_id"],item["attachment"],temporary)
            info=inspect_image(temporary,self.config.get("max_image_bytes",20*1024*1024))
            expected=text(item["fields"].get("SHA256")).lower()
            if expected and expected!=info["sha256"]:
                raise FactoryError("素材附件与已登记 SHA256 不一致，禁止继续。")
            ext={"PNG":"png","JPEG":"jpg","WEBP":"webp"}[info["format"]]
            dest=root/"inputs"/(safe_id(item["record_id"])+"_"+info["sha256"][:12]+"."+ext)
            shutil.move(str(temporary),dest)
            inputs.append({"record_id":item["record_id"],"type":text(item["fields"]["类型"]),
                           "path":str(dest.relative_to(root)),**info})
        if current["mode"]=="背景合成" and not next(x for x in inputs if x["type"]=="商品原图")["has_transparency"]:
            raise FactoryError("背景合成需要透明商品原图。请先抠图并人工核对，再上传为新素材版本。")
        run={"schema_version":1,"id":rid,"task_id":task_id,"base_token":self.base.token,
             "source":current,"source_digest":digest(current),"created_at":now(),"inputs":inputs,
             "calls_reserved":0,"slots":{},"reviews_seen":{},"cancelled":False,"is_demo":current["is_demo"]}
        refs=[x for x in inputs if current["mode"]=="参考图编辑" or x["type"]!="商品原图"]
        for number in range(1,current["count"]+1):
            sid=f"{number:03d}"
            prompt=compile_prompt(current,number)
            request={"run_id":rid,"slot":sid,"producer":"codex_native","image_count":1,
                     "prompt":prompt,"prompt_sha256":digest(prompt),"reference_images":refs,
                     "requested_size":current["size"],"api_fallback_authorized":False,
                     "note":"Only actual image-tool output counts; no screenshot, mock or stock replacement."}
            write_json(root/"requests"/(sid+".json"),request)
            run["slots"][sid]={"state":"PENDING","attempts":[],"request_digest":digest(request)}
        write_json(root/"source-snapshot.json",current)
        self.state.save(run)
        self.state.log("prepared",run_id=rid,count=current["count"])
        self._status(run,"待生图")
        self.archive(rid)
        return run

    def _slot(self,run,sid):
        if sid not in run["slots"]:
            raise FactoryError("槽位不存在。")
        return run["slots"][sid]

    def request(self,run,sid):
        root=self.run_root(run["id"])
        req=read_json(confined(root,"requests/"+sid+".json"))
        if digest(req)!=self._slot(run,sid)["request_digest"]:
            raise FactoryError("生成请求被修改。必须新建任务，不能悄悄改已冻结的 Prompt。")
        return req

    def start(self,run_id,sid):
        run=self.state.run(run_id);self.guard(run)
        slot=self._slot(run,sid)
        if slot["state"]!="PENDING":
            raise FactoryError("该槽位已经派发或完成。禁止重复生图；不确定结果需人工确认后 retry-slot。")
        if run["calls_reserved"]>=run["source"]["max_calls"]:
            raise FactoryError("已到用户批准的调用次数上限。禁止自动追加预算。")
        if not self.allow_demo:
            capability=self.state.root/"capability.json"
            if not capability.exists():
                raise FactoryError("尚未完成本机原生生图探针。先运行 prompts/01-SETUP.md 的验证环节。")
            cap=read_json(capability)
            if cap.get("producer")!="codex_native" or not cap.get("verified"):
                raise FactoryError("原生生图能力未验证，不能自动改走付费 API。")
        request=self.request(run,sid)
        attempt={"id":"try_"+uuid.uuid4().hex,"started_at":now(),"state":"UNCERTAIN_UNTIL_RECEIPT"}
        slot["attempts"].append(attempt);slot["state"]="STARTED"
        run["calls_reserved"]+=1
        self.state.save(run)  # Reserve BEFORE external generation.
        self.state.log("generation_reserved",run_id=run_id,slot=sid,attempt=attempt["id"])
        self._status(run,"生成中")
        self.archive(run_id)  # Remote checkpoint BEFORE the agent invokes image generation.
        dispatch={**request,"attempt_id":attempt["id"],"incoming_directory":str(self.run_root(run_id)/"incoming")}
        write_json(self.run_root(run_id)/"dispatch"/(sid+"_"+attempt["id"]+".json"),dispatch)
        return dispatch

    def retry_slot(self,run_id,sid,reason,ack_possible_charge):
        run=self.state.run(run_id);self.guard(run);slot=self._slot(run,sid)
        if slot["state"]!="STARTED" or not ack_possible_charge or not reason.strip():
            raise FactoryError("仅可对未收到有效回执的槽位进行人工重试授权；必须说明原因并确认可能已计费。")
        if run["calls_reserved"]>=run["source"]["max_calls"]:
            raise FactoryError("调用次数已达上限。保留记录，在飞书另建有新预算的任务。")
        slot["attempts"][-1]["state"]="ABANDONED_WITH_POSSIBLE_CHARGE"
        slot["attempts"][-1]["reason"]=reason
        slot["state"]="PENDING";self.state.save(run)
        self.state.log("human_retry_authorized",run_id=run_id,slot=sid,reason=reason)

    def receive(self,run_id,sid,receipt_path:Path):
        run=self.state.run(run_id);self.guard(run);slot=self._slot(run,sid)
        receipt=read_json(receipt_path)
        if slot["state"] in {"FINALIZED","UPLOADED"}:
            if digest(receipt)==slot.get("receipt_digest"):
                return slot
            raise FactoryError("已完成槽位收到不同回执，禁止覆盖。另建任务处理新版本。")
        if slot["state"]!="STARTED":
            raise FactoryError("先 start-slot 预留调用次数，再登记真实结果。")
        req=self.request(run,sid);attempt=slot["attempts"][-1]
        if any(receipt.get(k)!=v for k,v in {"run_id":run_id,"slot":sid,"attempt_id":attempt["id"],
                                           "prompt_sha256":req["prompt_sha256"]}.items()):
            raise FactoryError("回执不属于当前任务、槽位、Prompt 或调用尝试。")
        producer=receipt.get("producer")
        if producer not in ({"mock"} if self.allow_demo else {"codex_native"}):
            raise FactoryError("Producer 不匹配；模拟图和付费 API 不能冒充 Codex 原生输出。")
        expected=sorted(x["sha256"] for x in req["reference_images"])
        if sorted(receipt.get("input_sha256",[]))!=expected:
            raise FactoryError("回执引用素材与已冻结的输入不一致。")
        if not isinstance(receipt.get("actual_model"),str) or not receipt["actual_model"].strip():
            raise FactoryError("须记录实际模型；工具未报告时明确写 not_reported。")
        try:
            generated=datetime.fromisoformat(receipt["generated_at"])
            if generated.tzinfo is None:
                raise ValueError()
        except (ValueError,KeyError,TypeError) as exc:
            raise FactoryError("generated_at 必须包含时区。") from exc
        root=self.run_root(run_id)
        image=confined(root,receipt.get("image_path",""))
        info=inspect_image(image,self.config.get("max_image_bytes",20*1024*1024))
        evidence=confined(root,receipt.get("tool_evidence_path",""))
        if not 0 < evidence.stat().st_size <= 200000:
            raise FactoryError("工具执行摘要证据须为 1 至 200000 字节。不要存凭证或完整环境变量。")
        for other in run["slots"].values():
            if other.get("native_sha256")==info["sha256"]:
                raise FactoryError("不同槽位出现完全相同的原始输出。请核对是否重复搬运文件。")
        ext={"PNG":"png","JPEG":"jpg","WEBP":"webp"}[info["format"]]
        native=root/"native"/(sid+"_"+attempt["id"]+"."+ext)
        native.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(image,native)
        final=root/"final"/(sid+".png");final.parent.mkdir(parents=True,exist_ok=True)
        if run["source"]["mode"]=="背景合成":
            product=next(x for x in run["inputs"] if x["type"]=="商品原图")
            operation=compose(native,confined(root,product["path"]),final,tuple(run["source"]["size"]),run["source"]["fraction"])
        else:
            if [info["width"],info["height"]]!=run["source"]["size"]:
                raise FactoryError("参考图编辑输出尺寸不匹配。禁止自动裁掉商品；保留原图并人工处理。")
            from PIL import Image
            with Image.open(native) as img:
                img.convert("RGB").save(final,"PNG")
            operation={"operation":"format_normalization_only","semantic_review_required":True}
        final_info=inspect_image(final)
        receipt_rel="receipts/"+sid+"_"+attempt["id"]+".json"
        write_json(root/receipt_rel,receipt)
        evidence_rel="receipts/"+sid+"_"+attempt["id"]+".txt"
        atomic_write(root/evidence_rel,evidence.read_bytes())
        slot.update({"state":"FINALIZED","receipt_digest":digest(receipt),"receipt_path":receipt_rel,
                     "evidence_path":evidence_rel,"evidence_sha256":file_hash(root/evidence_rel),
                     "native_path":str(native.relative_to(root)),"native_sha256":info["sha256"],
                     "final_path":str(final.relative_to(root)),"final_sha256":final_info["sha256"],
                     "technical_check":final_info,"operation":operation,"producer":producer,
                     "actual_model":receipt["actual_model"],"asset_id":run_id+"_"+sid+"_"+attempt["id"][-8:]})
        attempt["state"]="RECEIVED";self.state.save(run)
        self.state.log("image_received",run_id=run_id,slot=sid,sha256=final_info["sha256"])
        return slot

    def _upload_once(self,table,record,field,source):
        sha=file_hash(source)
        key=f"upload:{table}:{record}:{field}:{source.name}:{sha}"
        def find():
            values=self.base.get_record(table,record)["fields"].get(field,[]) or []
            hits=[a for a in values if a.get("name")==source.name and a.get("size")==source.stat().st_size]
            if len(hits)>1:
                raise FactoryError("相同附件出现多份，先人工核对。")
            return hits[0] if hits else None
        return self.state.effect(key,find,lambda:self.base.upload_attachment(table,record,field,source))

    def push(self,run_id):
        run=self.state.run(run_id);self.guard(run);root=self.run_root(run_id)
        for sid,slot in run["slots"].items():
            if slot["state"] not in {"FINALIZED","UPLOADED"}:
                continue
            final=confined(root,slot["final_path"])
            if file_hash(final)!=slot["final_sha256"]:
                raise FactoryError("成图文件被修改，禁止覆盖既有图片版本。")
            aid=slot["asset_id"]
            record=self.state.effect("asset:"+aid,
                lambda:self.base.find_unique("assets","资产ID",aid),
                lambda:self.base.create_record("assets",{"素材名":aid,"资产ID":aid,"类型":["生成成图"],
                    "商品":[run["source"]["product_id"]],"来源任务":[run["task_id"]],"SHA256":slot["final_sha256"],
                    "示例数据":run["is_demo"],"运行ID":run_id,"槽位":sid,"审核状态":["待审核"],
                    "模式":[run["source"].get("task_mode","demo" if run["is_demo"] else "production")],
                    "资产角色":["candidate"],
                    "来源说明":"由冻结任务及真实调用回执生成，参见任务运行档案。","允许用于生图":False}))
            rr=record["record_id"]
            staging=root/"upload"/(slot["final_sha256"][:16]+"_"+sid+".png")
            staging.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(final,staging)
            self._upload_once("assets",rr,"图片",staging)
            self._upload_once("assets",rr,"生产回执",confined(root,slot["receipt_path"]))
            slot["asset_record_id"]=rr;slot["state"]="UPLOADED";self.state.save(run)
        complete=all(x["state"]=="UPLOADED" for x in run["slots"].values())
        self._status(run,"待审核" if complete else "生成中")
        self.archive(run_id)
        return run

    def decisions(self,run):
        """Read-only intake; live code never creates or edits review decisions."""
        allowed=set(self.config.get("reviewer_open_ids",[]))
        if self.allow_demo:
            allowed.add("ou_demo")
        if not allowed:
            raise FactoryError("尚未设置允许审核的用户 open_id。不能把 AI 自报审核当人工批准。")
        rows=self.base.list_records("reviews");decisions={}
        current_ids={r["record_id"] for r in rows}
        if not set(run["reviews_seen"]).issubset(current_ids):
            raise FactoryError("已归档审核记录被删除。先核对审计历史，禁止退回旧批准结论。")
        for sid,slot in run["slots"].items():
            rid=slot.get("asset_record_id")
            if not rid:
                continue
            matched=[]
            for row in rows:
                f=row["fields"]
                if rid not in links(f.get("图片资产")):
                    continue
                if len(links(f.get("图片资产")))!=1:
                    raise FactoryError("每条审核只能指向一个图片版本。")
                row_hash=digest(row)
                old=run["reviews_seen"].get(row["record_id"])
                if old and old!=row_hash:
                    raise FactoryError("已归档审核记录被修改。请恢复旧记录并新增审核，不要覆盖审计历史。")
                run["reviews_seen"][row["record_id"]]=row_hash
                people=links(f.get("审核人"))
                creators=f.get("创建人") or []
                if isinstance(creators,dict):
                    creators=[creators]
                creators=links(creators)
                if len(people)!=1 or people[0] not in allowed or creators!=people:
                    raise FactoryError("审核人不在允许名单，或与飞书系统创建人不一致。")
                if f.get("人工确认") is not True or text(f.get("目标SHA256"))!=slot["final_sha256"]:
                    raise FactoryError("审核缺少人工确认或指向错误图片哈希。")
                choice=text(f.get("结论"))
                if choice not in {"通过","驳回","撤回"}:
                    raise FactoryError("审核结论无效。")
                if choice=="通过" and not (f.get("商品准确") is True and f.get("品牌及渠道检查") is True):
                    raise FactoryError("批准前须完成人工商品、品牌和渠道检查。")
                timestamp=integer(f.get("创建时间"),"系统创建时间",1,9_999_999_999_999)
                matched.append((timestamp,row["record_id"],choice,row))
            if matched:
                matched.sort(key=lambda x:(x[0],x[1]))
                latest=matched[-1]
                if len({x[2] for x in matched if x[0]==latest[0]})>1:
                    raise FactoryError("同时出现冲突审核，需新增一条明确结论。")
                decisions[sid]={"decision":latest[2],"review_record":latest[3]}
        self.state.save(run)
        return decisions

    def sync_reviews(self,run_id):
        run=self.state.run(run_id);self.guard(run)
        decisions=self.decisions(run)
        for sid,d in decisions.items():
            self.base.update_record("assets",run["slots"][sid]["asset_record_id"],{"审核状态":[d["decision"]]})
        self.archive(run_id)
        return decisions

    def export(self,run_id,allow_partial=False):
        run=self.state.run(run_id);self.guard(run);root=self.run_root(run_id)
        decisions=self.decisions(run)
        if not allow_partial and (any(s["state"]!="UPLOADED" for s in run["slots"].values()) or len(decisions)!=len(run["slots"])):
            raise FactoryError("仍有未完成或未审核候选。全部处理后导出；部分交付须显式使用 --allow-partial。")
        selected={sid:s for sid,s in run["slots"].items() if decisions.get(sid,{}).get("decision")=="通过"}
        if not selected:
            raise FactoryError("没有人工批准的图片，拒绝生成空交付包。")
        entries={};manifest={"schema_version":1,"run_id":run_id,"is_demo":run["is_demo"],
            "requested_candidates":len(run["slots"]),"generated_candidates":sum(s["state"]=="UPLOADED" for s in run["slots"].values()),
            "delivered_count":len(selected),"partial_authorized":bool(allow_partial),
            "source_digest":run["source_digest"],"images":[],"cost":"unknown; native subscription use is not zero-cost proof"}
        for sid,slot in selected.items():
            row=self.base.get_record("assets",slot["asset_record_id"])
            f=row["fields"]
            if text(f.get("SHA256"))!=slot["final_sha256"] or text(f.get("资产ID"))!=slot["asset_id"]:
                raise FactoryError("远端资产版本元数据被修改。")
            attachments=f.get("图片") or []
            if len(attachments)!=1:
                raise FactoryError("成图附件缺失或数量不为一，不能交付。")
            verify=root/"verify"/(sid+".img")
            self.base.download_attachment("assets",row["record_id"],attachments[0],verify)
            if file_hash(verify)!=slot["final_sha256"]:
                raise FactoryError("飞书中的图片附件已变化，原审核失效。")
            local=confined(root,slot["final_path"])
            if file_hash(local)!=slot["final_sha256"]:
                raise FactoryError("本地成图被修改。")
            name="images/"+slot["asset_id"]+".png"
            entries[name]=local.read_bytes()
            manifest["images"].append({"filename":name,"asset_id":slot["asset_id"],"sha256":slot["final_sha256"],
                "sku":text(run["source"]["product"].get("SKU")),"workflow_version":text(run["source"]["flow"].get("版本")),
                "prompt_sha256":self.request(run,sid)["prompt_sha256"],"producer":slot["producer"],
                "actual_model":slot["actual_model"],"review_record_id":decisions[sid]["review_record"]["record_id"]})
        # Recheck approvals immediately before writing; this narrows but cannot eliminate remote races.
        fresh=self.decisions(run)
        if digest(decisions)!=digest(fresh):
            raise FactoryError("导出期间审核发生改变，停止交付并重新核对。")
        entries["manifest.json"]=(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n").encode()
        note="仅含已批准图片；未自动发布到任何电商平台。\n"
        if run["is_demo"]:
            note="DEMO ONLY：全部图片和业务数据都是模拟示例，非 TOPSTAR 实拍，禁止上架。\n"+note
        entries["READ-ME.txt"]=note.encode()
        entries["SHA256SUMS.txt"]=("\n".join(x["sha256"]+"  "+x["filename"] for x in manifest["images"])+"\n").encode()
        output=self.state.root/"deliveries"/(run_id+"_"+digest(manifest)[:12]+".zip")
        deterministic_zip(output,entries)
        self._upload_once("tasks",run["task_id"],"已批准交付包",output)
        run["last_export"]={"path":str(output.relative_to(self.state.root)),"sha256":file_hash(output),"manifest":manifest}
        self.state.save(run);self._status(run,"已完成" if not allow_partial else "待审核")
        self.state.log("exported",run_id=run_id,count=len(selected),sha256=file_hash(output))
        self.archive(run_id)
        return output,manifest

    def archive(self,run_id):
        run=self.state.run(run_id);root=self.run_root(run_id)
        entries={"run.json":(canonical(run)+"\n").encode()}
        paths={"source-snapshot.json"}
        paths.update(x["path"] for x in run["inputs"])
        for sid,slot in run["slots"].items():
            paths.add("requests/"+sid+".json")
            for key in ("native_path","final_path","receipt_path","evidence_path"):
                if slot.get(key):
                    paths.add(slot[key])
        for rel in sorted(paths):
            entries[rel]=confined(root,rel).read_bytes()
        if sum(len(data) for data in entries.values()) > self.config.get("max_archive_bytes",128*1024*1024):
            raise FactoryError("运行备份超过配置容量上限。禁止跳过备份继续派发；检查租户容量并拆小后续任务。")
        checks={name:__import__("hashlib").sha256(data).hexdigest() for name,data in entries.items()}
        entries["archive-checksums.json"]=(canonical(checks)+"\n").encode()
        name=run_id+"_internal_"+digest(checks)[:12]+".zip"
        target=self.state.root/"archives"/name
        deterministic_zip(target,entries)
        self._upload_once("tasks",run["task_id"],"运行档案",target)
        remote=self.base.get_record("tasks",run["task_id"])["fields"]
        matches=[a for a in remote.get("运行档案",[]) if a.get("name")==target.name and a.get("size")==target.stat().st_size]
        if len(matches)!=1 or not matches[0].get("file_token"):
            raise FactoryError("运行检查点尚未在飞书可见，禁止派发生图。先只读对账，不重复上传。")
        self.base.update_record("tasks",run["task_id"],{"最新档案SHA256":file_hash(target)})
        confirmed=self.base.get_record("tasks",run["task_id"])["fields"]
        if text(confirmed.get("最新档案SHA256"))!=file_hash(target):
            raise FactoryError("最新检查点指针回读失败，禁止派发生图。")
        return target

    def cancel(self,run_id):
        run=self.state.run(run_id);run["cancelled"]=True;self.state.save(run)
        self._status(run,"已取消","已发出的原生生图可能仍完成并消耗额度；不再派发新槽位。")
        self.archive(run_id)
        return run


def restore_archive(archive_path:Path,state:State,expected_base:str):
    """Restore to an empty run only; remote writes still require read reconciliation."""
    state.bind_base(expected_base)
    with zipfile.ZipFile(archive_path) as archive:
        info=archive.infolist()
        if len(info)>2000 or sum(x.file_size for x in info)>1024*1024*1024:
            raise FactoryError("备份超过恢复安全上限。")
        names=[x.filename for x in info]
        if len(names)!=len(set(names)):
            raise FactoryError("备份内有重复文件名。")
        for item in info:
            p=Path(item.filename)
            if p.is_absolute() or ".." in p.parts or "\\" in item.filename or ((item.external_attr>>16)&0o170000)==0o120000:
                raise FactoryError("备份包含越界路径或软链接。")
        if "run.json" not in names or "archive-checksums.json" not in names:
            raise FactoryError("不是本工程的运行备份。")
        checks=json.loads(archive.read("archive-checksums.json"))
        if set(checks)!=(set(names)-{"archive-checksums.json"}):
            raise FactoryError("备份清单不完整。")
        for name,sha in checks.items():
            if __import__("hashlib").sha256(archive.read(name)).hexdigest()!=sha:
                raise FactoryError("备份文件校验失败。")
        run=json.loads(archive.read("run.json"))
        if run.get("schema_version")!=1 or run.get("base_token")!=expected_base:
            raise FactoryError("备份版本或飞书 Base 不匹配。")
        safe_id(run["id"]);safe_id(run["task_id"])
        if state.by_task(run["task_id"]) is not None:
            raise FactoryError("已有此任务的执行记录，禁止用旧备份覆盖。")
        root=state.root/"runs"/run["id"]
        if root.exists() and any(root.iterdir()):
            raise FactoryError("运行目录非空，禁止覆盖恢复。")
        for item in info:
            target=confined(root,item.filename,must_exist=False)
            atomic_write(target,archive.read(item.filename))
        # Any dispatched image generation without a receipt stays STARTED/uncertain.
        state.save(run);state.log("restored",run_id=run["id"],archive_sha256=file_hash(archive_path))
        return run
