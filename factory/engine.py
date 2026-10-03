from __future__ import annotations
import copy
import io
import json
import re
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from .media import inspect_image, compose
from .model import sources, compile_prompt
from .schema import TASK_INPUTS
from .state import State
from .util import (FactoryError, canonical, confined, digest, file_hash, integer,
                   larkcli_tempdir, links, now, read_json, safe_id, text,
                   write_json, atomic_write)


_FORM_CREATE_CONFIRMATION = "确认创建演示任务"


def _is_form_create_confirmation(value):
    return (
        isinstance(value, str) and value == _FORM_CREATE_CONFIRMATION
    ) or (
        isinstance(value, list) and len(value) == 1 and
        isinstance(value[0], str) and value[0] == _FORM_CREATE_CONFIRMATION
    )


def _freeze_form_create_confirmation(value):
    if _is_form_create_confirmation(value):
        return _FORM_CREATE_CONFIRMATION
    return copy.deepcopy(value)


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

    def preview_form_record(self, task_id):
        """Validate the Feishu business form without writing or dispatching it.

        The profile is trusted local Engine configuration, never browser/form input.
        A successful preview is not an authorization to submit or produce images.
        """
        safe_id(task_id)
        profile = self._validated_intake_profile()
        raw = self.base.get_record("tasks", task_id)["fields"]
        business_fields = {
            "图片用途", "消费场景", "视觉风格", "附加要求", "数量", "创建确认",
        }
        controlled = (set(TASK_INPUTS) - business_fields - {"任务名"}) | {
            "提交", "取消", "系统状态", "运行ID", "错误摘要",
            "最新档案SHA256", "运行档案", "已批准交付包",
        }
        if any(raw.get(name) is not None and raw.get(name) != "" and
               raw.get(name) != [] and raw.get(name) is not False for name in controlled):
            raise FactoryError("表单记录含系统技术字段，不能按普通用户任务受理。")
        return self._form_source_from_fields(task_id, raw, profile)

    def _validated_intake_profile(self):
        profile = self.config.get("intake_profile")
        required = ("allowed_product_ids", "workflow_id", "channel", "placement",
                    "namespace", "mode", "review_policy_version", "max_calls")
        if not isinstance(profile, dict) or any(key not in profile for key in required):
            raise FactoryError("受信任务受理配置不存在或不完整。")
        scope = profile["allowed_product_ids"]
        if not isinstance(scope, list) or any(not isinstance(item, str) for item in scope):
            raise FactoryError("受信任务受理配置的商品范围必须是记录 ID 列表。")
        mode, policy = profile["mode"], profile["review_policy_version"]
        if ((mode == "demo" and (policy != "demo-v2" or
             not text(profile["namespace"]).startswith("V1-DEMO-KIDS"))) or
            (mode == "production" and policy != "production-v1") or
            mode not in {"demo", "production"}):
            raise FactoryError("受信任务受理配置的模式、命名空间与审核策略不匹配。")
        max_calls = integer(profile["max_calls"], "图片调用上限", 1,
                            6 if mode == "demo" else self.config.get("max_calls_per_task", 18))
        return profile

    def _form_business_fields(self, raw):
        names = ("选择商品", "图片用途", "消费场景", "视觉风格", "数量", "附加要求")
        fields = {name: copy.deepcopy(raw.get(name)) for name in names}
        fields["创建确认"] = _freeze_form_create_confirmation(raw.get("创建确认"))
        return fields

    def _form_selected_product(self, raw, profile):
        choice = raw.get("选择商品")
        if isinstance(choice, list) and len(choice) == 1:
            choice = choice[0]
        if not isinstance(choice, str) or not choice.strip():
            raise FactoryError("请选择一个商品。")
        product = self.base.find_unique_typed("products", "商品名", choice.strip())
        if not product or product["record_id"] not in profile["allowed_product_ids"]:
            raise FactoryError("该商品不在当前受理范围内。")
        return choice.strip(), product

    def _form_source_from_fields(self, task_id, raw, profile):
        max_calls = integer(
            profile["max_calls"], "图片调用上限", 1,
            6 if profile["mode"] == "demo" else self.config.get("max_calls_per_task", 18),
        )
        choice, product = self._form_selected_product(raw, profile)
        task = {
            "任务名": "生图任务：" + choice.strip(),
            "商品": [product["record_id"]],
            "流程": [safe_id(profile["workflow_id"])],
            "渠道": text(profile["channel"]),
            "图片用途": raw.get("图片用途"),
            "消费场景": raw.get("消费场景"),
            "版位": text(profile["placement"]),
            "视觉风格": raw.get("视觉风格"),
            "附加要求": raw.get("附加要求"),
            "数量": raw.get("数量"),
            "创建确认": _freeze_form_create_confirmation(raw.get("创建确认")),
            "最多调用次数": max_calls,
            "提交": True,
            "取消": False,
            "示例数据": profile["mode"] == "demo",
            "命名空间": text(profile["namespace"]),
            "模式": text(profile["mode"]),
            "审核策略版本": text(profile["review_policy_version"]),
        }
        source = sources(self.base, task_id, self.config.get("max_images_per_task",12),
                         self.config.get("max_calls_per_task",18),
                         allow_demo=self.allow_demo or profile["mode"] == "demo",
                         task_fields=task)
        want_demo = profile["mode"] == "demo"
        members = [source["product"], source["flow"]] + [a["fields"] for a in source["assets"]]
        if any((member.get("示例数据") is True) != want_demo for member in members):
            raise FactoryError("商品、流程与素材的 Demo/Production 模式必须与任务一致。")
        with larkcli_tempdir(prefix="image-factory-intake-") as temp:
            for asset in source["assets"]:
                path = Path(temp) / safe_id(asset["record_id"])
                self.base.download_attachment("assets", asset["record_id"],
                                              asset["attachment"], path)
                info = inspect_image(path, self.config.get("max_image_bytes", 20*1024*1024))
                expected = text(asset["fields"].get("SHA256")).lower()
                if not re.fullmatch(r"[0-9a-f]{64}", expected):
                    raise FactoryError("素材缺少有效 SHA256，禁止受理。")
                if expected != info["sha256"]:
                    raise FactoryError("素材附件与已登记 SHA256 不一致，禁止受理。")
                if (source["mode"] == "背景合成" and
                    text(asset["fields"].get("类型")) == "商品原图" and
                    not info["has_transparency"]):
                    raise FactoryError("背景合成需要透明商品原图；白底图不能直接受理。")
        return source

    def _form_task_projection(self, source, profile, run_id):
        fields = copy.deepcopy(source["task"])
        fields.update({
            "系统状态": ["待生图"], "运行ID": run_id, "错误摘要": "",
            "命名空间": profile["namespace"], "模式": ["demo"],
            "审核策略版本": "demo-v2", "运行版本": 0,
            "提交": True, "取消": False,
        })
        return fields

    def _form_projection_matches(self, actual, expected):
        if not isinstance(actual, dict):
            return False
        for name, wanted in expected.items():
            got = actual.get(name)
            if name in {"商品", "流程", "选用素材", "返工来源", "选用提示词组件"}:
                equal = links(got) == links(wanted)
            elif isinstance(wanted, list) and all(isinstance(item, str) for item in wanted):
                equal = text(got) == "".join(wanted)
            elif isinstance(wanted, str):
                equal = text(got) == wanted
            elif isinstance(wanted, (int, float)) and not isinstance(wanted, bool):
                equal = (isinstance(got, (int, float)) and not isinstance(got, bool)
                         and float(got) == float(wanted))
            else:
                equal = got == wanted
            if not equal:
                return False
        return True

    def _read_form_active_pointer(self, pointer_path):
        if pointer_path.is_symlink():
            raise FactoryError("active-v1-run 指针不能是软链接。")
        if not pointer_path.exists():
            return None
        if not pointer_path.is_file():
            raise FactoryError("active-v1-run 指针必须是普通文件。")
        pointer = read_json(pointer_path)
        if (not isinstance(pointer, dict) or
                set(pointer) != {"run_id", "authorization_id"}):
            raise FactoryError("active-v1-run 指针格式无效。")
        safe_id(pointer.get("run_id", ""))
        safe_id(pointer.get("authorization_id", ""))
        return pointer

    def _form_runtime_plan(self, source, profile, task_id, grant, run_id,
                           business_fields, task_projection):
        product_assets = [asset for asset in source["assets"]
                          if text(asset["fields"].get("类型")) == "商品原图"]
        if len(product_assets) != 1:
            raise FactoryError("表单受理需要唯一的默认商品原图。")
        product_asset = product_assets[0]
        expected_sha = text(product_asset["fields"].get("SHA256")).lower()
        relative_snapshot = (
            "references/form-intake/" + run_id + "-" +
            safe_id(product_asset["record_id"]) + "-" + expected_sha + ".png"
        )
        snapshot = confined(self.state.root, relative_snapshot)
        from .image_pipeline import validate_product
        _, snapshot_info = validate_product(snapshot)
        if snapshot_info["sha256"] != expected_sha:
            raise FactoryError("商品原图快照 SHA256 与当前素材不一致。")

        template_path = Path(__file__).resolve().parents[1] / "templates" / "runtime-workflow-kids-demo-v1.json"
        plan = read_json(template_path)
        plan["run_id"] = run_id
        plan["max_image_calls"] = source["max_calls"]
        plan["authorization_id"] = safe_id(grant["authorization_id"])
        plan["authorization_scope"] = copy.deepcopy(grant["scope"])
        plan["form_task_record_id"] = task_id
        plan["intake_binding"] = {
            "form_task_record_id": task_id,
            "business_fields": copy.deepcopy(business_fields),
            "business_fields_sha256": digest(business_fields),
            "source_sha256": digest(source),
            "profile_sha256": digest(profile),
            "grant_sha256": digest(grant),
            "task_projection": copy.deepcopy(task_projection),
        }
        plan["steps"][0] = {
            "id": "product-source",
            "kind": "compose",
            "depends_on": [],
            "operation": "import_product_source",
            "reference_asset": {
                "local_path": relative_snapshot,
                "sha256": expected_sha,
                # Runtime output.source_asset_id is a Feishu record ID.
                "asset_id": product_asset["record_id"],
                "business_asset_id": text(product_asset["fields"].get("资产ID")),
            },
        }
        background_steps = [step for step in plan["steps"]
                            if step.get("id") in {
                                "background-outdoor", "background-indoor", "background-studio"
                            }]
        if len(background_steps) != 3:
            raise FactoryError("受信 Runtime 模板必须恰有 3 个背景步骤。")
        composition_directions = (
            "宽景构图：镜头稍远，环境留白更多，商品摆放区清楚完整。",
            "中景构图：环境细节适中，商品摆放区位于画面中部偏下。",
            "极简构图：减少背景装饰，突出干净平整的商品摆放区。",
        )
        for index, (step, direction) in enumerate(zip(background_steps, composition_directions), 1):
            step["visual_prompt"] = compile_prompt(source, index) + "\n\n" + direction
        composition = [step for step in plan["steps"]
                       if step.get("id") == "candidate-composition"]
        if len(composition) != 1 or composition[0].get("expected_outputs") != 3:
            raise FactoryError("受信 Runtime 模板必须保留 3 候选合成节点。")
        return plan

    def _validated_form_grant(self, profile, grant, minimum_calls, product_record_id):
        if not isinstance(grant, dict) or grant.get("approved") is not True:
            raise FactoryError("表单受理需要显式批准的受信授权。")
        authorization_id = safe_id(grant.get("authorization_id", ""))
        actor_id = grant.get("actor_id")
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise FactoryError("受信授权必须明确记录授权人。")
        safe_id(actor_id)
        scope = grant.get("scope")
        category = profile.get("category", "kids_shoes")
        if (not isinstance(scope, dict) or
                scope.get("namespace") != profile["namespace"] or
                scope.get("mode") != profile["mode"] or
                scope.get("category") != category or
                category != "kids_shoes"):
            raise FactoryError("受信授权的命名空间、模式或品类与本地受理配置不一致。")
        if scope.get("product_record_id") != product_record_id:
            raise FactoryError("受信授权未绑定表单当前选中的商品。")
        limit = grant.get("project_image_calls_limit")
        if type(limit) is not int or limit < minimum_calls:
            raise FactoryError("受信授权的图片调用上限不足任务预算。")
        return authorization_id, scope

    def _reconcile_form_admission(self, task_id, raw, profile, grant,
                                  authorization_id, run_id):
        from .runtime import Runtime
        from .runtime_runner import runtime_db

        runtime = Runtime(runtime_db(self.state.root), single_instance=True)
        try:
            row = runtime.db.execute(
                "SELECT plan_hash FROM runtime_runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise FactoryError("表单显示已有运行ID，但本地 Runtime 没有对应冻结运行。")
            task_run_value = text(raw.get("运行ID"))
            if task_run_value not in (None, "", run_id):
                raise FactoryError("飞书任务行自称绑定其他运行，不能重用现有 Runtime。")
            task_written = task_run_value == run_id
            plan = runtime.plan(run_id)
            status = runtime.status(run_id)
            if digest(plan) != row[0]:
                raise FactoryError("Runtime 冻结 plan 与账本 SHA256 不一致。")
            if (status["state"] != "queued" or status["image_calls_reserved"] != 0 or
                    status["next"].get("step") != "product-source"):
                raise FactoryError("已受理 Runtime 不再处于零调用 queued 状态，不能按表单重试。")
            other_active = runtime.db.execute(
                "SELECT id FROM runtime_runs WHERE id<>? AND state<>'completed' LIMIT 1",
                (run_id,),
            ).fetchone()
            if other_active:
                raise FactoryError("存在另一条未完成 Runtime；不能恢复表单指针。")
            binding = plan.get("intake_binding")
            if (not isinstance(binding, dict) or
                    plan.get("form_task_record_id") != task_id or
                    binding.get("form_task_record_id") != task_id or
                    plan.get("run_id") != run_id or
                    plan.get("authorization_id") != authorization_id or
                    plan.get("authorization_scope") != grant.get("scope") or
                    binding.get("profile_sha256") != digest(profile) or
                    binding.get("grant_sha256") != digest(grant)):
                raise FactoryError("重试表单的授权或受信配置与冻结 Runtime 不一致。")
            if task_written and raw.get("命名空间") != profile["namespace"]:
                raise FactoryError("飞书任务行命名空间与受信配置不一致。")

            business_fields = self._form_business_fields(raw)
            if (business_fields != binding.get("business_fields") or
                    digest(business_fields) != binding.get("business_fields_sha256")):
                raise FactoryError("已受理表单的业务字段发生变化，不能替换冻结运行。")
            source = self._form_source_from_fields(task_id, raw, profile)
            self._validated_form_grant(
                profile, grant, source["max_calls"], source["product_id"],
            )
            if digest(source) != binding.get("source_sha256"):
                raise FactoryError("商品、流程或素材来源与冻结受理时不一致。")
            task_projection = self._form_task_projection(source, profile, run_id)
            if task_projection != binding.get("task_projection"):
                raise FactoryError("冻结任务投影与 Runtime 绑定不一致。")
            plan_expected = self._form_runtime_plan(
                source, profile, task_id, grant, run_id,
                business_fields, task_projection,
            )
            if plan != plan_expected:
                raise FactoryError("Runtime plan 与当前表单、授权或业务来源不一致。")

            if task_written:
                for name in ("最新档案SHA256", "运行档案", "已批准交付包"):
                    value = raw.get(name)
                    if value is not None and value != "" and value != [] and value is not False:
                        raise FactoryError("飞书任务行含冻结投影之外的技术字段，停止重试。")
                if not self._form_projection_matches(raw, task_projection):
                    raise FactoryError("飞书任务行与冻结 Runtime 投影不一致。")

            pointer_path = self.state.root / "active-v1-run.json"
            active_pointer = {"run_id": run_id, "authorization_id": authorization_id}
            active_before = self._read_form_active_pointer(pointer_path)
            if active_before is not None and active_before != active_pointer:
                previous_run_id = active_before["run_id"]
                previous_status = runtime.status(previous_run_id)
                previous_grant = runtime.registered_dispatch_authorization(previous_run_id)
                if (previous_status["state"] != "completed" or
                        previous_grant.get("authorization_id") != active_before["authorization_id"]):
                    raise FactoryError("active-v1-run 指向未完成或授权不一致的 Runtime。")

            registered = runtime.registered_dispatch_authorization(run_id)
            if (registered.get("approved") is not True or
                    registered.get("authorization_id") != authorization_id or
                    registered.get("scope") != grant.get("scope") or
                    registered.get("project_image_calls_limit") != grant.get("project_image_calls_limit") or
                    registered.get("actor_id") != grant.get("actor_id")):
                raise FactoryError("Runtime 中登记的授权与本次受信授权不一致。")

            pointer_effect_key = f"form-intake:active-v1-run:{run_id}"

            def find_active_pointer():
                return (active_pointer if
                        self._read_form_active_pointer(pointer_path) == active_pointer
                        else None)

            def write_missing_pointer():
                if self._read_form_active_pointer(pointer_path) != active_before:
                    raise FactoryError("active-v1-run 在恢复期间发生变化，拒绝覆盖。")
                write_json(pointer_path, active_pointer)
                return active_pointer

            self.state.effect(pointer_effect_key, find_active_pointer, write_missing_pointer)
            if self._read_form_active_pointer(pointer_path) != active_pointer:
                raise FactoryError("Runtime 与 active-v1-run 指针不一致。")
        finally:
            runtime.close()

        record_key = f"form-intake:task:{task_id}:{run_id}"
        effect_row = self.state.db.execute(
            "SELECT state FROM effects WHERE key=?", (record_key,)
        ).fetchone()
        effect_state = effect_row[0] if effect_row else None
        if not task_written and effect_state == "done":
            raise FactoryError("State.effect 已确认任务写回，但 Feishu 行缺少对应运行ID。")
        if not task_written and effect_state is None:
            business_names = {
                "选择商品", "图片用途", "消费场景", "视觉风格", "数量", "附加要求",
                "创建确认",
            }
            controlled = (set(TASK_INPUTS) - business_names - {"任务名"}) | {
                "提交", "取消", "系统状态", "运行ID", "错误摘要",
                "最新档案SHA256", "运行档案", "已批准交付包",
            }
            if any(raw.get(name) is not None and raw.get(name) != "" and
                   raw.get(name) != [] and raw.get(name) is not False for name in controlled):
                raise FactoryError("Runtime 已冻结但任务行含部分或冲突写回，停止恢复。")

        def find_projection():
            current = self.base.get_record("tasks", task_id)
            if self._form_projection_matches(current["fields"], task_projection):
                return current
            return None

        self.state.effect(
            record_key,
            find_projection,
            lambda: self.base.update_record("tasks", task_id, task_projection),
        )
        if not self._form_projection_matches(
                self.base.get_record("tasks", task_id)["fields"], task_projection):
            raise FactoryError("飞书任务投影在重试读回时发生变化。")
        return {
            "run_id": run_id,
            "task_record_id": task_id,
            "product_record_id": source["product_id"],
            "workflow_record_id": source["flow_id"],
            "runtime_result": "existing",
            "status": status,
        }

    def admit_form_record(self, task_id, grant):
        """Queue the validated demo form on the V1 Runtime without dispatching.

        ``grant`` is supplied by a trusted caller. None of its authorization
        fields are read from the form record.
        """
        safe_id(task_id)
        profile = self._validated_intake_profile()
        if profile["mode"] != "demo" or profile["review_policy_version"] != "demo-v2":
            raise FactoryError("表单受理目前只支持 demo-v2、背景合成和 3 个候选。")
        if profile["namespace"] != "V1-DEMO-KIDS":
            raise FactoryError("表单受理的命名空间必须精确为 V1-DEMO-KIDS，避免下游投影无法识别。")
        run_id = "V1-DEMO-KIDS-" + digest({
            "namespace": profile["namespace"], "form_record_id": task_id,
        })[:24]
        safe_id(run_id)
        initial_record = self.base.get_record("tasks", task_id)
        initial_raw = initial_record["fields"]
        _, selected_product = self._form_selected_product(initial_raw, profile)
        authorization_id, _ = self._validated_form_grant(
            profile, grant, profile["max_calls"], selected_product["record_id"],
        )

        from .runtime import Runtime
        from .runtime_runner import runtime_db
        runtime = Runtime(runtime_db(self.state.root), single_instance=True)
        try:
            exists = runtime.db.execute(
                "SELECT 1 FROM runtime_runs WHERE id=?", (run_id,)
            ).fetchone() is not None
            other_active = runtime.db.execute(
                "SELECT id FROM runtime_runs WHERE id<>? AND state<>'completed' LIMIT 1",
                (run_id,),
            ).fetchone()
        finally:
            runtime.close()
        if exists or initial_raw.get("运行ID"):
            return self._reconcile_form_admission(
                task_id, initial_raw, profile, grant, authorization_id, run_id,
            )
        if other_active:
            raise FactoryError("已有未完成 Runtime；先恢复或完成原任务，不能另建表单运行。")

        if not _is_form_create_confirmation(initial_raw.get("创建确认")):
            raise FactoryError("请先选择“确认创建演示任务”后再创建任务。")

        pointer_effect = self.state.db.execute(
            "SELECT key FROM effects WHERE state='unknown' AND "
            "(key='form-intake:active-v1-run' OR key LIKE 'form-intake:active-v1-run:%') "
            "LIMIT 1"
        ).fetchone()
        if pointer_effect:
            raise FactoryError(
                "active-v1-run 存在结果不确定的写入；先只读核对原运行，不得开始新运行。"
            )

        business_fields = self._form_business_fields(initial_raw)
        source = self.preview_form_record(task_id)
        current_raw = self.base.get_record("tasks", task_id)["fields"]
        if self._form_business_fields(current_raw) != business_fields:
            raise FactoryError("表单业务字段在预览期间发生变化，请重新确认后提交。")
        if (source["mode"] != "背景合成" or source["count"] != 3 or
                source["task_mode"] != "demo" or source["category"] != "kids_shoes"):
            raise FactoryError("表单受理目前只支持 demo-v2、背景合成和 3 个候选。")
        self._validated_form_grant(
            profile, grant, source["max_calls"], source["product_id"],
        )

        pointer_path = self.state.root / "active-v1-run.json"
        active_before = self._read_form_active_pointer(pointer_path)
        active_pointer = {"run_id": run_id, "authorization_id": authorization_id}
        if active_before is not None and active_before != active_pointer:
            if active_before["authorization_id"] == authorization_id:
                raise FactoryError("后续表单必须使用明确的新受信授权，不能复用旧 Runtime 授权。")
            previous_run_id = active_before["run_id"]
            runtime = Runtime(runtime_db(self.state.root), single_instance=True)
            try:
                previous_status = runtime.status(previous_run_id)
                previous_grant = runtime.registered_dispatch_authorization(previous_run_id)
                if (previous_status["state"] != "completed" or
                        previous_grant.get("authorization_id") != active_before["authorization_id"]):
                    raise FactoryError(
                        "已有 active-v1-run 尚未完成或授权不一致，不能受理另一表单。"
                    )
            finally:
                runtime.close()

        product_asset = next(asset for asset in source["assets"]
                             if text(asset["fields"].get("类型")) == "商品原图")
        expected_sha = text(product_asset["fields"].get("SHA256")).lower()
        relative_snapshot = (
            "references/form-intake/" + run_id + "-" +
            safe_id(product_asset["record_id"]) + "-" + expected_sha + ".png"
        )
        snapshot = confined(self.state.root, relative_snapshot, must_exist=False)
        with tempfile.TemporaryDirectory(prefix=".form-intake-", dir=str(self.state.root)) as temp:
            downloaded = Path(temp) / "product-source"
            self.base.download_attachment("assets", product_asset["record_id"],
                                          product_asset["attachment"], downloaded)
            downloaded_info = inspect_image(
                downloaded, self.config.get("max_image_bytes", 20 * 1024 * 1024))
            if (downloaded_info["sha256"] != expected_sha or
                    downloaded_info["format"] != "PNG" or
                    not downloaded_info["has_transparency"]):
                raise FactoryError("商品原图快照必须与登记 SHA256 一致且为真实透明 PNG。")

            if snapshot.exists():
                if not snapshot.is_file() or file_hash(snapshot) != expected_sha:
                    raise FactoryError("商品原图快照路径已有不同文件，禁止覆盖。")
            else:
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                snapshot = confined(self.state.root, relative_snapshot, must_exist=False)
                try:
                    with downloaded.open("rb") as source_file, snapshot.open("xb") as target_file:
                        shutil.copyfileobj(source_file, target_file)
                except FileExistsError:
                    if snapshot.is_symlink() or not snapshot.is_file() or file_hash(snapshot) != expected_sha:
                        raise FactoryError("商品原图快照并发冲突，禁止覆盖。")
                except Exception:
                    if snapshot.exists() and file_hash(snapshot) != expected_sha:
                        snapshot.unlink()
                    raise

        from .image_pipeline import validate_product
        _, snapshot_info = validate_product(snapshot)
        if snapshot_info["sha256"] != expected_sha:
            raise FactoryError("商品原图快照写入后 SHA256 不一致。")

        task_projection = self._form_task_projection(source, profile, run_id)
        plan = self._form_runtime_plan(
            source, profile, task_id, grant, run_id,
            business_fields, task_projection,
        )

        runtime = Runtime(runtime_db(self.state.root), single_instance=True)
        try:
            created = runtime.create(plan, authorization=grant)
            status = runtime.status(run_id)
            if status["state"] != "queued" or status["image_calls_reserved"] != 0:
                raise FactoryError("Runtime 未保持 queued 且零调用预算状态，停止表单写回。")
        finally:
            runtime.close()

        pointer_effect_key = f"form-intake:active-v1-run:{run_id}"

        def find_active_pointer():
            return (active_pointer if
                    self._read_form_active_pointer(pointer_path) == active_pointer
                    else None)

        def write_active_pointer():
            if self._read_form_active_pointer(pointer_path) != active_before:
                raise FactoryError("active-v1-run 在新运行排队期间发生变化，拒绝覆盖。")
            write_json(pointer_path, active_pointer)
            return active_pointer

        self.state.effect(
            pointer_effect_key,
            find_active_pointer,
            write_active_pointer,
        )
        if self._read_form_active_pointer(pointer_path) != active_pointer:
            raise FactoryError("active-v1-run 指针写入后读回不一致。")

        self.state.effect(
            f"form-intake:task:{task_id}:{run_id}",
            lambda: (self.base.get_record("tasks", task_id)
                     if self._form_projection_matches(
                         self.base.get_record("tasks", task_id)["fields"], task_projection
                     ) else None),
            lambda: self.base.update_record("tasks", task_id, task_projection),
        )
        if not self._form_projection_matches(
                self.base.get_record("tasks", task_id)["fields"], task_projection):
            raise FactoryError("表单任务技术字段写入后读回不一致。")
        return {
            "run_id": run_id,
            "task_record_id": task_id,
            "product_record_id": source["product_id"],
            "workflow_record_id": source["flow_id"],
            "runtime_result": created,
            "status": status,
        }

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
