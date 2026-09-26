from __future__ import annotations
import copy
import json
from pathlib import Path
from .schema import TABLES
from .util import FactoryError, UnknownWrite, canonical, digest, file_hash, links, read_json, text, write_json
from .metrics import parse_metrics
from .model import one


def schema_plan(base):
    """Return an exact read-only additive migration plan for the configured Base."""
    additions=[];drift=[];present=[]
    for key,spec in TABLES.items():
        actual={field["field_name"]:field for field in base.list_fields(key)}
        for wanted in spec["fields"]:
            item=actual.get(wanted["field_name"])
            label=f"{key}.{wanted['field_name']}"
            if item is None:
                additions.append({"key":label,"table":key,"field":copy.deepcopy(wanted)})
                continue
            present.append(label)
            if item.get("type") != wanted["type"]:
                drift.append({"key":label,"reason":"type","actual":item.get("type"),"expected":wanted["type"]})
                continue
            if wanted.get("target") and item.get("property",{}).get("table_id") != base.tables[wanted["target"]]:
                drift.append({"key":label,"reason":"linked_table"})
            if wanted["type"] == 3:
                actual_options={option.get("name") for option in item.get("property",{}).get("options",[])}
                missing=sorted({option["name"] for option in wanted["property"]["options"]}-actual_options)
                if missing:
                    drift.append({"key":label,"reason":"missing_select_options","missing":missing})
    return {"base":base.token,"additions":additions,"drift":drift,"present_count":len(present),
            "policy":"只新增 additions；任何 drift 都先停止，不删除、不改名、不覆盖。"}


def apply_schema_additions(base, state):
    """Apply only the fields returned by :func:`schema_plan`, with readback.

    This migration never creates/deletes tables and never updates records.  Each
    field is journaled independently so an ambiguous remote result halts before
    the next addition.  A local checkpoint contains display names only.
    """
    plan=schema_plan(base)
    if plan["drift"]:
        raise FactoryError("V1 schema 存在字段漂移，停止增量迁移。")
    checkpoint_dir=state.root/"schema-migrations";checkpoint_dir.mkdir(parents=True,exist_ok=True)
    checkpoint={"schema_version":1,"state":"running","base_bound":True,
                "planned":[item["key"] for item in plan["additions"]],"applied":[]}
    write_json(checkpoint_dir/"v1-latest.json",checkpoint)
    for item in plan["additions"]:
        key=item["table"]
        desired=copy.deepcopy(item["field"])
        if "target" in desired:
            target=desired.pop("target");multiple=desired.pop("multiple")
            desired["property"]={"table_id":base.tables[target],"multiple":multiple}
        def find(key=key,desired=desired):
            matches=[field for field in base.list_fields(key)
                     if field.get("field_name")==desired["field_name"]]
            if len(matches)>1:
                raise FactoryError(f"{item['key']} 出现同名字段，停止迁移。")
            if not matches:
                return None
            actual=matches[0]
            if actual.get("type")!=desired["type"]:
                raise FactoryError(f"{item['key']} 类型读回不一致。")
            if desired.get("property",{}).get("table_id") and actual.get("property",{}).get("table_id")!=desired["property"]["table_id"]:
                raise FactoryError(f"{item['key']} 关联表读回不一致。")
            return actual
        state.effect(f"v1-schema:{base.token}:{item['key']}",find,
                     lambda key=key,desired=desired:base.create_field(key,desired))
        if find() is None:
            raise FactoryError(f"{item['key']} 写入后无法读回，停止迁移。")
        checkpoint["applied"].append(item["key"])
        write_json(checkpoint_dir/"v1-latest.json",checkpoint)
    after=schema_plan(base)
    if after["additions"] or after["drift"]:
        raise FactoryError("V1 schema 迁移后契约未收敛。")
    checkpoint["state"]="complete"
    checkpoint["verified_present_count"]=after["present_count"]
    write_json(checkpoint_dir/"v1-latest.json",checkpoint)
    return {"applied":checkpoint["applied"],"checkpoint":str(checkpoint_dir/"v1-latest.json"),
            "verified_present_count":after["present_count"]}


def reconcile_schema_invalid_parameter(base, state, field_key: str, error_code: str):
    """Authorize one schema retry after a proven validation rejection.

    This is deliberately narrower than the generic ``resolve-effect`` path:
    only the observed lark validation code is accepted, the field must still
    be in the current additive plan, and the exact migration effect must be
    pending.  The original event history remains; only the unknown retry guard
    is cleared through State's audited resolution method.
    """
    if error_code != "800010701":
        raise FactoryError("只接受已核实的 lark invalid-parameter code=800010701。")
    plan=schema_plan(base)
    if plan["drift"]:
        raise FactoryError("schema 存在 drift，禁止协调 unknown migration。")
    additions={item["key"] for item in plan["additions"]}
    if field_key not in additions:
        raise FactoryError("目标字段并非当前确认缺失的 addition，禁止重试。")
    effect=f"v1-schema:{base.token}:{field_key}"
    if effect not in state.pending_effects():
        raise FactoryError("目标 schema effect 不是 pending unknown。")
    state.resolve_effect(effect,True)
    state.log("schema_invalid_parameter_reconciled",field=field_key,error_code=error_code,
              basis="remote field absent and structured validation rejection")
    return {"retry_authorized":effect,"field":field_key,"error_code":error_code,
            "remote_absence_verified":True}


def bootstrap(base,state,config_path:Path,apply=False):
    plan={"base":base.token,"tables":[{"name":v["name"],"field_count":len(v["fields"])} for v in TABLES.values()],
          "policy":"只新增本工程命名的表和缺少的字段；不删除、不改名、不改已有字段类型。"}
    if not apply:
        return plan
    def list_tables():
        return base.list_tables()
    def find_table(name):
        rows=[r for r in list_tables() if r.get("name")==name]
        if len(rows)>1:
            raise FactoryError("存在同名数据表，请人工消歧。")
        return rows[0] if rows else None
    for key,spec in TABLES.items():
        def create_table(spec=spec):
            result=base.create_table(spec["name"],[spec["fields"][0]])
            if not isinstance(result,dict) or not (result.get("table_id") or result.get("table",{}).get("table_id")):
                raise UnknownWrite("创建表返回缺少 table_id，先核对远端，不能标记创建成功。")
            return result
        row=state.effect("schema:"+base.token+":table:"+key,lambda spec=spec:find_table(spec["name"]),create_table)
        tid=row.get("table_id") or row.get("table",{}).get("table_id")
        if not tid:
            raise FactoryError("建表响应未给出 table_id，先核对远端。")
        base.tables[key]=tid
        config=read_json(config_path);config["tables"]=base.tables;write_json(config_path,config)
    for key,spec in TABLES.items():
        for original in spec["fields"]:
            desired=copy.deepcopy(original)
            if "target" in desired:
                target=desired.pop("target");multiple=desired.pop("multiple")
                desired["property"]={"table_id":base.tables[target],"multiple":multiple}
            def find_field(desired=desired,key=key):
                matches=[f for f in base.list_fields(key) if f.get("field_name")==desired["field_name"]]
                if len(matches)>1:
                    raise FactoryError("存在同名字段。")
                if matches:
                    if matches[0].get("type")!=desired["type"]:
                        raise FactoryError("已有字段类型不匹配，不自动改变或覆盖。")
                    return matches[0]
                return None
            state.effect("schema:"+base.token+":field:"+key+":"+desired["field_name"],find_field,
                lambda key=key,desired=desired:base.create_field(key,desired))
    return {**plan,"applied":True,"table_ids":base.tables}


def schema_check(base):
    errors=[]
    for key,spec in TABLES.items():
        actual={f["field_name"]:f for f in base.list_fields(key)}
        for want in spec["fields"]:
            got=actual.get(want["field_name"])
            if not got or got.get("type")!=want["type"]:
                errors.append(key+"."+want["field_name"]+": missing/type drift")
            elif want.get("target") and got.get("property",{}).get("table_id")!=base.tables[want["target"]]:
                errors.append(key+"."+want["field_name"]+": wrong linked table")
            elif want["type"]==3:
                names={x.get("name") for x in got.get("property",{}).get("options",[])}
                missing={x["name"] for x in want["property"]["options"]}-names
                if missing:
                    errors.append(key+"."+want["field_name"]+": missing select options")
    if errors:
        raise FactoryError("字段契约检查失败："+"；".join(errors))
    return {"ok":True,"tables_checked":len(TABLES)}


def setup_views(base,state):
    specifications=[
        ("products","商品卡片","gallery","商品照片",["商品名","SKU","商品品牌","资料已确认"]),
        ("assets","素材画册","gallery","图片",["素材名","类型","商品","审核状态","允许用于生图"]),
        ("tasks","生产看板","kanban",None,["任务名","命名空间","模式","商品","选用提示词组件","数量","系统状态","错误摘要"]),
        ("steps","按流程编排","grid",None,["步骤名","组件ID","组件版本","流程","顺序","节点类型","提示词模板","参考素材"]),
        ("reviews","审图台","grid",None,["审核标题","图片资产","结论","Demo视觉检查","仅限演示使用","商品准确","品牌及渠道检查","人工确认","原因"]),
    ]
    result=[]
    for key,name,kind,cover,visible in specifications:
        def find(key=key,name=name):
            rows=base.list_views(key)
            hits=[x for x in rows if x.get("view_name")==name]
            if len(hits)>1:
                raise FactoryError("视图重名，停止自动配置。")
            return hits[0] if hits else None
        row=state.effect(f"view:{base.token}:{key}:{name}",find,
            lambda key=key,name=name,kind=kind:base.create_view(key,name,kind))
        view_id=row.get("view_id") or row.get("view",{}).get("view_id")
        if not view_id:
            raise FactoryError("未能解析 view_id。保留已建视图，使用 larkcli --help 核对版本。")
        base.set_view_visible_fields(key,view_id,visible)
        if cover:
            base.set_view_card(key,view_id,cover)
        if key=="tasks":
            base.set_view_group(key,view_id,[{"field":"系统状态","desc":False}])
        if key=="steps":
            base.set_view_group(key,view_id,[{"field":"流程","desc":False}])
            base.set_view_sort(key,view_id,[{"field":"顺序","desc":False}])
        result.append({"table":key,"name":name,"view_id":view_id})
    return result


_SEED_SELECT_FIELDS={"生产方式","节点类型"}


def _normalize_seed_field(field, value):
    if value is None:
        return ""
    if field in _SEED_SELECT_FIELDS:
        if isinstance(value, list):
            normalized=[]
            for item in value:
                if isinstance(item, str):
                    normalized.append(item)
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    normalized.append(item["text"])
                else:
                    return value
            return normalized
        return [value]
    if field=="流程":
        return links(value)
    if isinstance(value, list) and all(isinstance(item,dict) and "text" in item for item in value):
        return text(value)
    return value


def _seed_find(base, table, field, value, expected):
    return base.find_unique_typed(table,field,value,field_names=list(expected))


def _seed_readback(base, table, field, value, expected, label):
    record=_seed_find(base,table,field,value,expected)
    if record is None:
        raise FactoryError(f"{label} 写入后只读读回不到记录。")
    actual=record.get("fields",{})
    for name,want in expected.items():
        if canonical(_normalize_seed_field(name,actual.get(name))) != canonical(_normalize_seed_field(name,want)):
            raise FactoryError(f"{label} 已存在但字段 {name} 不一致，停止静默覆盖。")
    return record


def seed_workflows(base,state,template:Path):
    payload=read_json(template)
    name=payload["workflow"]["流程名"]
    flow_expected=payload["workflow"]
    flow=state.effect("seed-flow:"+base.token+":"+name,
        lambda:_seed_find(base,"workflows","流程名",name,flow_expected),
        lambda:base.create_record("workflows",flow_expected))
    flow=_seed_readback(base,"workflows","流程名",name,flow_expected,"流程种子")
    flow_id=flow.get("record_id")
    if not isinstance(flow_id,str):
        raise FactoryError("流程种子读回缺少 record_id。")
    for s in payload["steps"]:
        fields={**s,"流程":[flow_id]}
        step_name=fields["步骤名"]
        step=state.effect("seed-step:"+base.token+":"+step_name,
            lambda step_name=step_name,fields=fields:_seed_find(base,"steps","步骤名",step_name,fields),
            lambda fields=fields:base.create_record("steps",fields))
        _seed_readback(base,"steps","步骤名",step_name,fields,"流程步骤种子")
    return flow


def _upload_evidence(base,state,table,record,field,path):
    key=f"evidence:{table}:{record}:{file_hash(path)}"
    def find():
        matches=[a for a in base.get_record(table,record)["fields"].get(field,[]) if a.get("name")==path.name and a.get("size")==path.stat().st_size]
        if len(matches)>1:
            raise FactoryError("证据附件重复。")
        return matches[0] if matches else None
    return state.effect(key,find,lambda:base.upload_attachment(table,record,field,path))


def import_metrics(base,state,csv_path:Path,revision=1,confirm_correction=False):
    if csv_path.stat().st_size>10*1024*1024:
        raise FactoryError("单次指标导入限制为 10MB，请按渠道日期拆分。")
    raw=csv_path.read_bytes();rows=parse_metrics(raw.decode("utf-8-sig"))
    source_sha=file_hash(csv_path)
    if not isinstance(revision,int) or not 1<=revision<=10000:
        raise FactoryError("版本必须为 1 至 10000 的整数。")
    old_rows=base.list_records("metrics");plans=[]
    for row in rows:
        if row["source_kind"]=="demo" and not base.is_mock:
            raise FactoryError("模拟指标不能进入真实效果数据表。")
        placement=base.find_unique("placements","使用ID",row["placement_id"])
        if not placement:
            raise FactoryError("缺少对应上架记录，不能把店铺总指标归给一张图片。")
        pf=placement["fields"]
        if pf.get("人工确认已上线") is not True or text(pf.get("渠道"))!=row["channel"]:
            raise FactoryError("上架记录未确认或渠道不匹配。")
        asset=base.get_record("assets",one(pf.get("图片资产"),"上架图片"))
        if text(pf.get("目标SHA256"))!=text(asset["fields"].get("SHA256")):
            raise FactoryError("上架记录与图片版本不一致。")
        if asset["fields"].get("示例数据") is True and not base.is_mock:
            raise FactoryError("模拟图片不能进入真实渠道指标。")
        previous=[r for r in old_rows if text(r["fields"].get("逻辑键"))==row["key"]]
        previous.sort(key=lambda r:r["fields"].get("版本",0))
        latest=previous[-1] if previous else None
        fields={"逻辑键":row["key"],"版本":revision,"使用记录":[placement["record_id"]],"日期":row["date"],
            "时区":row["timezone"],"渠道":row["channel"],"曝光":row["impressions"],"点击":row["clicks"],
            "订单":row["orders"],"点击率":row["ctr"],"每点击订单率":row["orders_per_click"],
            "指标口径":row["metric_definition"],"数据性质":row["source_kind"],"来源文件SHA256":source_sha,
            "样本提醒":row["sample_note"],"模式":["production"]}
        if latest:
            lf=latest["fields"]
            compare=("使用记录","日期","时区","渠道","曝光","点击","订单","指标口径","数据性质")
            if all(lf.get(k)==fields.get(k) for k in compare):
                plans.append({"skip":latest});continue
            if not confirm_correction or revision!=lf["版本"]+1:
                raise FactoryError("同一天的已有数据发生变化。更正须 --confirm-correction 且版本为上一版加一，保留旧证据。")
            fields["取代指标ID"]=text(lf.get("指标ID"))
        elif revision!=1:
            raise FactoryError("第一版指标必须使用版本 1。")
        metric_id="metric_"+row["key"][:20]+"_v"+str(revision)
        fields.update({"指标ID":metric_id,"指标记录名":row["placement_id"]+" "+row["date"]+" v"+str(revision)})
        plans.append({"fields":fields})
    # Complete validation before the first write. Writes remain individually journaled.
    evidence=state.root/"imports"/(source_sha+".csv")
    evidence.parent.mkdir(parents=True,exist_ok=True);evidence.write_bytes(raw)
    result=[]
    for plan in plans:
        if "skip" in plan:
            record=plan["skip"]
            # Still reconcile missing original evidence after an interrupted prior import.
            original_sha=text(record["fields"].get("来源文件SHA256"))
            candidate=state.root/"imports"/(original_sha+".csv")
            if candidate.exists():
                _upload_evidence(base,state,"metrics",record["record_id"],"来源文件",candidate)
            result.append({"record_id":record["record_id"],"status":"existing"});continue
        fields=plan["fields"];mid=fields["指标ID"]
        record=state.effect("metric:"+mid,lambda mid=mid:base.find_unique("metrics","指标ID",mid),
                            lambda fields=fields:base.create_record("metrics",fields))
        _upload_evidence(base,state,"metrics",record["record_id"],"来源文件",evidence)
        result.append({"record_id":record["record_id"],"status":"created"})
    state.log("metrics_imported",source_sha256=source_sha,rows=len(rows),revision=revision)
    return result


def _is_demo_metric(base, row: dict) -> bool:
    fields = row["fields"]
    if text(fields.get("数据性质")) in {"demo", "模拟数据"}:
        return True
    usage_ids = links(fields.get("使用记录"))
    if len(usage_ids) != 1:
        return False
    try:
        usage = base.get_record("placements", usage_ids[0])
        asset_ids = links(usage["fields"].get("图片资产"))
        if len(asset_ids) != 1:
            return False
        asset = base.get_record("assets", asset_ids[0])
    except FactoryError:
        return False
    return asset["fields"].get("示例数据") is True


def metric_summary(base, include_demo=False):
    latest={};seen_versions=set()
    for row in base.list_records("metrics"):
        if not include_demo:
            # Formal statistics are an explicit allowlist. Unknown/legacy mode is
            # not silently treated as production merely because it is not demo.
            if text(row.get("fields",{}).get("模式")) != "production":
                continue
        f=row["fields"];key=text(f.get("逻辑键"));version=f.get("版本",0)
        if not key:
            raise FactoryError("存在无逻辑键指标，不能安全去重。")
        if (key,version) in seen_versions:
            raise FactoryError("同一指标出现重复版本。")
        seen_versions.add((key,version))
        if key not in latest or version>latest[key]["fields"].get("版本",0):
            latest[key]=row
    return [{"record_id":r["record_id"],"fields":r["fields"]} for r in latest.values()]


def _probe_fields(marker: str, updated: bool = False) -> dict:
    return {
        "素材名": f"[IF] Feishu CRUD probe {marker}",
        "资产ID": marker,
        "类型": ["风格参考"],
        "示例数据": True,
        "允许用于生图": False,
        "来源说明": "连接探针已更新" if updated else "仅连接探针",
        "授权说明": "不用于商品生产",
    }


def _probe_lookup(base, marker, field_names):
    return base.find_unique_typed("assets", "资产ID", marker, field_names=field_names)


def probe_feishu(base, state):
    """Verify typed Base access and CRUD without creating an image asset."""
    directory = state.root / "feishu-probe"
    directory.mkdir(parents=True, exist_ok=True)
    workflow = base.find_unique_typed("workflows", "流程名", "[IF] 背景合成 v1.0",
                                      field_names=["流程名", "版本", "生产方式", "已发布"])
    if not workflow:
        raise FactoryError("正式 Workflow 不可读，连接探针停止。")
    flow_id = workflow["record_id"]
    workflow_read = base.get_record("workflows", flow_id,
                                    ["流程名", "版本", "生产方式", "已发布"])
    if workflow_read["record_id"] != flow_id:
        raise FactoryError("正式 Workflow 读回 ID 不一致。")
    steps = base.list_records("steps", ["步骤名", "流程", "顺序", "节点类型", "启用"])
    linked_steps = [row for row in steps if links(row["fields"].get("流程")) == [flow_id]]
    if len(linked_steps) != 6:
        raise FactoryError(f"正式 Workflow Steps 关联数量不符：{len(linked_steps)}。")
    products = base.list_records("products", ["商品名"], limit=1)

    from uuid import uuid4
    run_id = uuid4().hex[:16]
    marker = f"IF-FEISHU-CRUD-PROBE-{run_id}"
    field_names = ["素材名", "资产ID", "类型", "示例数据", "允许用于生图", "来源说明", "授权说明"]
    created = state.effect(
        f"probe-crud-create:{base.token}:{marker}",
        lambda: _probe_lookup(base, marker, field_names),
        lambda: base.create_record("assets", _probe_fields(marker)),
    )
    record_id = created.get("record_id") if isinstance(created, dict) else None
    if not isinstance(record_id, str):
        raise FactoryError("CRUD 探针创建未返回 record_id。")
    read = base.get_record("assets", record_id, field_names)
    if read["fields"].get("资产ID") != marker:
        raise FactoryError("CRUD 探针创建后读回标记不一致。")

    updated_fields = {"来源说明": "连接探针已更新"}
    updated = state.effect(
        f"probe-crud-update:{base.token}:{marker}",
        lambda: _probe_lookup(base, marker, field_names)
        if (_probe_lookup(base, marker, field_names) or {}).get("fields", {}).get("来源说明") == "连接探针已更新"
        else None,
        lambda: base.update_record("assets", record_id, updated_fields),
    )
    read = base.get_record("assets", record_id, field_names)
    if read["fields"].get("来源说明") != "连接探针已更新":
        raise FactoryError("CRUD 探针更新后读回不一致。")

    state.effect(
        f"probe-crud-delete:{base.token}:{marker}",
        lambda: True if _probe_lookup(base, marker, ["资产ID"]) is None else None,
        lambda: base.delete_record("assets", record_id),
    )
    if _probe_lookup(base, marker, ["资产ID"]) is not None:
        raise FactoryError("CRUD 探针删除后仍可读到记录。")
    report = {
        "base_token": base.token,
        "probe_marker": marker,
        "record_id": record_id,
        "project_table_read": True,
        "project_table_sample_count": len(products),
        "workflow_read": True,
        "workflow_steps_read": len(linked_steps) == 6,
        "relation_verified": True,
        "record_crud": True,
        "record_absent_after_delete": True,
        "is_live": not base.is_mock,
        "image_model_called": False,
    }
    write_json(directory / "result.json", report)
    return report


def probe_attachments(base, state):
    """Upload and download a non-image probe file, then remove its record."""
    from uuid import uuid4
    directory = state.root / "feishu-probe"
    directory.mkdir(parents=True, exist_ok=True)
    run_id = uuid4().hex[:16]
    marker = f"IF-FEISHU-ATTACHMENT-PROBE-{run_id}"
    source = directory / f"feishu-attachment-probe-{run_id}.bin"
    source.write_bytes(("Image-Factory attachment probe\n" + run_id + "\n").encode("utf-8"))
    downloaded = directory / f"feishu-attachment-probe-{run_id}.download.bin"
    fields = {
        "素材名": f"[IF] Attachment probe {run_id}",
        "资产ID": marker,
        "类型": ["风格参考"],
        "示例数据": True,
        "允许用于生图": False,
        "来源说明": "附件往返探针",
        "授权说明": "不用于商品生产",
    }
    names = ["素材名", "资产ID", "图片"]
    created = state.effect(
        f"probe-attachment-create:{base.token}:{marker}",
        lambda: _probe_lookup(base, marker, names),
        lambda: base.create_record("assets", fields),
    )
    record_id = created.get("record_id") if isinstance(created, dict) else None
    if not isinstance(record_id, str):
        raise FactoryError("附件探针创建未返回 record_id。")
    base.get_record("assets", record_id, names)
    state.effect(
        f"probe-attachment-upload:{base.token}:{marker}",
        lambda: next((item for item in (base.get_record("assets", record_id, ["图片"])["fields"].get("图片") or [])
                      if item.get("name") == source.name and item.get("size") == source.stat().st_size), None),
        lambda: base.upload_attachment("assets", record_id, "图片", source),
    )
    remote = base.get_record("assets", record_id, ["资产ID", "图片"])
    attachments = remote["fields"].get("图片") or []
    matches = [item for item in attachments if item.get("name") == source.name and item.get("size") == source.stat().st_size]
    if len(matches) != 1:
        raise FactoryError("附件上传后飞书记录中未找到唯一匹配附件。")
    base.download_attachment("assets", record_id, matches[0], downloaded)
    original_size = source.stat().st_size
    downloaded_size = downloaded.stat().st_size if downloaded.is_file() else -1
    original_sha = file_hash(source)
    downloaded_sha = file_hash(downloaded) if downloaded.is_file() else ""
    if original_size != downloaded_size or original_sha != downloaded_sha:
        raise FactoryError("附件下载内容与原始文件不一致。")
    state.effect(
        f"probe-attachment-delete:{base.token}:{marker}",
        lambda: True if _probe_lookup(base, marker, ["资产ID"]) is None else None,
        lambda: base.delete_record("assets", record_id),
    )
    if _probe_lookup(base, marker, ["资产ID"]) is not None:
        raise FactoryError("附件探针记录删除后仍可读到。")
    report = {
        "base_token": base.token,
        "probe_marker": marker,
        "record_id": record_id,
        "filename": source.name,
        "original_byte_length": original_size,
        "downloaded_byte_length": downloaded_size,
        "original_sha256": original_sha,
        "downloaded_sha256": downloaded_sha,
        "bytes_equal": original_size == downloaded_size,
        "sha256_equal": original_sha == downloaded_sha,
        "record_absent_after_delete": True,
        "is_live": not base.is_mock,
    }
    write_json(directory / "attachment-result.json", report)
    return report
