from __future__ import annotations
import json
import os
import re
import signal
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from .util import FactoryError, UnknownWrite, canonical, safe_id, text

class LarkCLI:
    """Authenticated transport. All auth stays inside the already-configured CLI."""
    def __init__(self, config: dict):
        candidate = config.get("lark_cli") or os.environ.get("LARK_CLI_BIN")
        self.bin = candidate or shutil.which("lark-cli") or shutil.which("larkcli")
        if not self.bin or not shutil.which(self.bin):
            raise FactoryError("未找到 lark-cli/larkcli。填入 config.local.json 的 lark_cli，保留你现有登录。")
        self.identity = config.get("identity", "user")
        if self.identity not in {"user", "bot"}:
            raise FactoryError("identity 只允许 user 或 bot，不自动切换身份。")
        self.timeout = int(config.get("cli_timeout_seconds", 120))
        self.attachment_timeout = int(config.get("attachment_timeout_seconds", 300))
        if self.attachment_timeout < self.timeout:
            raise FactoryError("attachment_timeout_seconds 不能短于 cli_timeout_seconds。")
        self.last_diagnostic: dict[str, Any] | None = None

    @staticmethod
    def _output_summary(raw: str | bytes | None) -> dict[str, Any]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not raw:
            return {"kind": "empty"}
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return {"kind": "non_json", "length": len(raw)}
        if not isinstance(value, dict):
            return {"kind": type(value).__name__}
        error = value.get("error") if isinstance(value.get("error"), dict) else {}
        data = value.get("data") if isinstance(value.get("data"), dict) else {}
        return {
            "kind": "json",
            "ok": value.get("ok"),
            "code": error.get("code", value.get("code")),
            "message": error.get("message", value.get("message")),
            "error_type": error.get("type"),
            "data_keys": sorted(str(key) for key in data),
        }

    @staticmethod
    def decode(stdout: str, stderr: str, code: int) -> Any:
        raw = stdout if code == 0 else stderr
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise FactoryError("larkcli 输出不是兼容 JSON。查看版本和 --help，勿猜命令；日志内容未自动暴露。") from exc
        if not isinstance(obj,dict):
            raise FactoryError("larkcli JSON 响应必须是对象。")
        if code != 0 or obj.get("ok") is False:
            err = obj.get("error", {})
            safe_code = str(err.get("code", "unknown"))[:40] if isinstance(err,dict) else "unknown"
            raise FactoryError(f"larkcli 调用失败，code={safe_code}。检查授权、资源权限、附件权限或网络；不自动扩大权限。")
        if obj.get("ok") is True:
            data = obj.get("data", {})
        elif obj.get("code") == 0:  # legacy raw OpenAPI envelope
            data = obj.get("data", {})
        else:
            raise FactoryError("larkcli 成功响应结构不兼容。")
        # Some versions wrap the raw response again. Never ignore inner errors.
        if isinstance(data, dict) and "code" in data:
            if data["code"] != 0:
                raise FactoryError(f"飞书接口失败，code={data['code']}。")
            data = data.get("data", {})
        return data

    @staticmethod
    def user_from_auth_status(value: Any) -> dict[str, Any]:
        """Extract only a verified current user from ``auth status --verify``."""
        if not isinstance(value, dict) or value.get("identity") != "user":
            raise FactoryError("Human UI 需要当前 lark-cli 的 user 身份。")
        identities = value.get("identities")
        user = identities.get("user") if isinstance(identities, dict) else None
        if (
            not isinstance(user, dict)
            or user.get("available") is not True
            or user.get("status") != "ready"
            or user.get("tokenStatus") != "valid"
        ):
            raise FactoryError("当前飞书 user 会话未通过在线验证。")
        open_id = user.get("openId")
        if not isinstance(open_id, str) or not open_id.strip():
            raise FactoryError("当前飞书 user 会话没有可信 open_id。")
        name = user.get("userName")
        return {
            "open_id": safe_id(open_id),
            "name": name.strip() if isinstance(name, str) else "",
            "verified": True,
        }

    def current_user(self) -> dict[str, Any]:
        """Verify the already-authorized user without logging in or changing auth."""
        if self.identity != "user":
            raise FactoryError("Human UI 不允许使用 bot 身份代替人工审核人。")
        argv = [self.bin, "auth", "status", "--json", "--verify"]
        for attempt in range(2):
            try:
                result = subprocess.run(
                    argv, shell=False, capture_output=True, text=True, timeout=self.timeout
                )
            except subprocess.TimeoutExpired as exc:
                raise FactoryError("验证当前飞书用户身份超时；未启动 Human UI。") from exc
            except OSError as exc:
                raise FactoryError("无法启动 lark-cli 验证当前用户身份。") from exc
            try:
                value = json.loads(result.stdout if result.returncode == 0 else result.stderr)
            except (TypeError, ValueError) as exc:
                raise FactoryError("lark-cli 身份状态不是兼容 JSON。") from exc
            if result.returncode != 0:
                raise FactoryError("当前飞书 user 会话在线验证失败。")
            try:
                return self.user_from_auth_status(value)
            except FactoryError as exc:
                if attempt or str(exc) != "当前飞书 user 会话未通过在线验证。":
                    raise
                time.sleep(0.5)
        raise AssertionError("unreachable")

    @staticmethod
    def _run_process_group(argv: list[str], timeout: int):
        process = subprocess.Popen(
            argv, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr) from exc
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)

    def call(self, args: list[str], mutation: bool = False, help_only: bool = False,
             timeout_seconds: int | None = None, process_group: bool = False) -> Any:
        argv = [self.bin, *args]
        if not help_only:
            argv += ["--as", self.identity, "--format", "json"]
        timeout = self.timeout if timeout_seconds is None else int(timeout_seconds)
        started = time.monotonic()
        try:
            if process_group:
                result = self._run_process_group(argv, timeout)
            else:
                result = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            self.last_diagnostic = {
                "command": args[:2],
                "timeout_seconds": timeout,
                "duration_seconds": round(time.monotonic() - started, 3),
                "exit_code": None,
                "timed_out": True,
                "process_group_terminated": process_group,
                "stdout": self._output_summary(exc.stdout),
                "stderr": self._output_summary(exc.stderr),
            }
            error = UnknownWrite if mutation else FactoryError
            raise error("larkcli 超时。写操作按结果不确定处理，禁止直接重复执行。") from exc
        except OSError as exc:
            raise FactoryError("无法启动 larkcli。") from exc
        self.last_diagnostic = {
            "command": args[:2],
            "timeout_seconds": timeout,
            "duration_seconds": round(time.monotonic() - started, 3),
            "exit_code": result.returncode,
            "timed_out": False,
            "process_group_terminated": False,
            "stdout": self._output_summary(result.stdout),
            "stderr": self._output_summary(result.stderr),
        }
        if help_only:
            if result.returncode:
                raise FactoryError("当前 larkcli 缺少所需命令；请用已安装版本的 --help 适配。")
            return result.stdout
        try:
            return self.decode(result.stdout, result.stderr, result.returncode)
        except FactoryError as exc:
            if mutation:
                summaries = (self.last_diagnostic or {}).get("stdout", {}), (self.last_diagnostic or {}).get("stderr", {})
                if any(item.get("error_type") == "validation" for item in summaries):
                    raise
                raise UnknownWrite(str(exc)) from exc
            raise

    def call_attachment(self, args: list[str], mutation: bool = False) -> Any:
        return self.call(
            args,
            mutation=mutation,
            timeout_seconds=self.attachment_timeout,
            process_group=True,
        )

class FeishuGateway:
    """唯一的 Base 访问层；业务代码不直接拼接 Feishu API 路径。"""
    is_mock = False
    _TYPE_NUMBERS = {
        "text": 1, "number": 2, "select": 3, "multi_select": 4,
        "datetime": 5, "checkbox": 7, "user": 11, "attachment": 17,
        "link": 18, "lookup": 19, "formula": 20, "created_at": 1001, "created_by": 1003,
    }
    _TYPE_NAMES = {value:key for key,value in _TYPE_NUMBERS.items()}

    def __init__(self, config: dict, transport=None):
        self.config = config
        self.token = safe_id(config.get("base_token", ""))
        self.tables = dict(config.get("tables", {}))
        self.cli = transport or LarkCLI(config)
        self.root = f"/open-apis/bitable/v1/apps/{self.token}"
        self.last_attachment_upload_diagnostic: dict[str, Any] | None = None
        delays = config.get("attachment_readback_delays_seconds", [0, 1, 2, 4])
        if not isinstance(delays, list) or not delays or any(not isinstance(value, (int, float)) or value < 0 for value in delays):
            raise FactoryError("attachment_readback_delays_seconds 必须是非负数字数组。")
        self.attachment_readback_delays = tuple(float(value) for value in delays)

    def table_path(self, table: str) -> str:
        if table not in self.tables:
            raise FactoryError(f"缺少表映射 {table}，先 bootstrap。")
        return f"{self.root}/tables/{safe_id(self.tables[table])}"

    def current_user(self) -> dict[str, Any]:
        resolver = getattr(self.cli, "current_user", None)
        if not callable(resolver):
            raise FactoryError("当前 Gateway transport 不能验证飞书用户身份。")
        return resolver()

    @staticmethod
    def _repeat(flag: str, values: list[str]) -> list[str]:
        result=[]
        for value in values:
            result += [flag,value]
        return result

    @staticmethod
    def _matrix_records(data: Any) -> list[dict]:
        if not isinstance(data, dict):
            raise FactoryError("typed record 响应不是对象。")
        columns=data.get("fields")
        rows=data.get("data")
        record_ids=data.get("record_id_list")
        if not isinstance(columns,list) or not all(isinstance(x,str) for x in columns):
            raise FactoryError("typed record 响应缺少字段列定义。")
        if not isinstance(rows,list) or not isinstance(record_ids,list) or len(rows)!=len(record_ids):
            raise FactoryError("typed record 响应行与 record ID 数量不一致。")
        result=[]
        for row,record_id in zip(rows,record_ids):
            if not isinstance(row,list) or not isinstance(record_id,str):
                raise FactoryError("typed record 响应包含不兼容的记录结构。")
            result.append({"record_id":safe_id(record_id),
                           "fields":{name:row[index] for index,name in enumerate(columns) if index<len(row)}})
        return result

    def list_records(self, table: str, field_names: list[str] | None = None,
                     filter_json: dict | str | None = None, limit: int = 200) -> list[dict]:
        if not isinstance(limit,int) or not 1<=limit<=200:
            raise FactoryError("typed record-list limit 必须为 1 至 200。")
        result=[];offset=0
        while True:
            args=["base","+record-list","--base-token",self.token,
                  "--table-id",self.tables[table],"--limit",str(limit),"--offset",str(offset)]
            if field_names:
                args += self._repeat("--field-id",field_names)
            if filter_json is not None:
                args += ["--filter-json",filter_json if isinstance(filter_json,str) else canonical(filter_json)]
            data=self.cli.call(args)
            result.extend(self._matrix_records(data))
            if not data.get("has_more",False):
                return result
            if not data.get("data"):
                raise FactoryError("typed record-list 分页无进展。")
            offset += len(data["data"])

    def get_record(self, table: str, record_id: str, field_names: list[str] | None = None) -> dict:
        args=["base","+record-get","--base-token",self.token,
              "--table-id",self.tables[table],"--record-id",safe_id(record_id)]
        if field_names:
            args += self._repeat("--field-id",field_names)
        rows=self._matrix_records(self.cli.call(args))
        if len(rows)!=1 or rows[0]["record_id"]!=record_id:
            raise FactoryError("typed record-get 未返回唯一目标记录。")
        return rows[0]

    def create_record(self, table: str, fields: dict) -> dict:
        data=self.cli.call(["base","+record-batch-create","--base-token",self.token,
                            "--table-id",self.tables[table],"--json",
                            canonical({"create_records":[fields]})],mutation=True)
        ids=data.get("record_id_list") if isinstance(data,dict) else None
        if not isinstance(ids,list) or len(ids)!=1 or not isinstance(ids[0],str):
            raise UnknownWrite("typed 记录创建未返回唯一 record_id，必须核对远端。")
        return {"record_id":safe_id(ids[0]),"fields":fields}

    def update_record(self, table: str, record_id: str, fields: dict) -> dict:
        data=self.cli.call(["base","+record-batch-update","--base-token",self.token,
                            "--table-id",self.tables[table],"--json",
                            canonical({"update_records":{safe_id(record_id):fields}})],mutation=True)
        return {"record_id":record_id,"fields":fields,"response":data}

    def delete_record(self, table: str, record_id: str):
        return self.cli.call(["base","+record-delete","--base-token",self.token,
                              "--table-id",self.tables[table],"--record-id",safe_id(record_id),"--yes"],mutation=True)

    def find_unique(self, table: str, field: str, value: str):
        hits=[r for r in self.list_records(table,field_names=[field],
                 filter_json={"logic":"and","conditions":[[field,"==",value]]})
               if text(r.get("fields",{}).get(field))==value]
        if len(hits)>1:
            raise FactoryError(f"{table}.{field} 出现重复值，停止自动归并。")
        return hits[0] if hits else None

    def find_unique_typed(self, table: str, field: str, value: str, field_names: list[str] | None = None):
        names=[]
        for name in [field,*(field_names or [])]:
            if name not in names:names.append(name)
        hits=[r for r in self.list_records(table,field_names=names,
                 filter_json={"logic":"and","conditions":[[field,"==",value]]})
               if text(r.get("fields",{}).get(field))==value]
        if len(hits)>1:
            raise FactoryError(f"{table}.{field} 出现重复值，停止自动归并。")
        return hits[0] if hits else None

    def _normalize_field(self, field: dict) -> dict:
        type_name=field.get("type")
        result={"field_id":field.get("id",field.get("field_id")),
                "field_name":field.get("name",field.get("field_name")),
                "type":self._TYPE_NUMBERS.get(type_name,type_name)}
        if type_name=="link" or field.get("link_table"):
            result["property"]={"table_id":field.get("link_table",field.get("property",{}).get("table_id")),
                                  "multiple":field.get("multiple",field.get("property",{}).get("multiple",False))}
        elif field.get("options") is not None:
            result["property"]={"options":[{"name":x.get("name")} for x in field["options"]]}
        elif isinstance(field.get("property"),dict):
            result["property"]=field["property"]
        return result

    def list_fields(self, table: str) -> list[dict]:
        data=self.cli.call(["base","+field-list","--base-token",self.token,
                            "--table-id",self.tables[table]])
        fields=data.get("fields") if isinstance(data,dict) else None
        if not isinstance(fields,list):
            raise FactoryError("typed field-list 响应缺少 fields。")
        return [self._normalize_field(field) for field in fields]

    def _typed_field(self, field: dict) -> dict:
        kind=self._TYPE_NAMES.get(field.get("type"),field.get("type"))
        result={"name":field["field_name"],"type":kind}
        prop=field.get("property",{})
        if kind=="select":
            result["multiple"]=bool(field.get("multiple",prop.get("multiple",False)))
            result["options"]=prop.get("options",[])
        elif kind=="link":
            target=field.get("target") or prop.get("table_id")
            result["link_table"]=self.tables.get(target,target)
            # lark-cli 1.0.95's typed field-create contract does not accept a
            # ``multiple`` key for link fields.  Link cells remain multi-value
            # in record data; creation only needs the target table here.
        elif prop:
            result.update({key:value for key,value in prop.items() if key not in {"options","table_id"}})
        return result

    def list_tables(self) -> list[dict]:
        data=self.cli.call(["base","+table-list","--base-token",self.token])
        rows=data.get("tables") if isinstance(data,dict) else None
        if not isinstance(rows,list):
            raise FactoryError("typed table-list 响应缺少 tables。")
        return [{"table_id":row.get("id",row.get("table_id")),"name":row.get("name"),**row} for row in rows]

    def create_table(self, name: str, fields: list[dict]):
        data=self.cli.call(["base","+table-create","--base-token",self.token,"--name",name,
                            "--fields",canonical([self._typed_field(field) for field in fields])],mutation=True)
        if not isinstance(data,dict):
            raise UnknownWrite("typed table-create 返回结构不兼容，必须核对远端。")
        row=data.get("table") if isinstance(data.get("table"),dict) else data
        table_id=row.get("id",row.get("table_id")) if isinstance(row,dict) else None
        return {**data,"table_id":table_id} if table_id else data

    def create_field(self, table: str, field: dict):
        return self.cli.call(["base","+field-create","--base-token",self.token,
                              "--table-id",self.tables[table],"--json",canonical(self._typed_field(field))],mutation=True)

    def list_views(self, table: str) -> list[dict]:
        data=self.cli.call(["base","+view-list","--base-token",self.token,
                            "--table-id",self.tables[table]])
        rows=data.get("views") if isinstance(data,dict) else None
        if not isinstance(rows,list):
            raise FactoryError("typed view-list 响应缺少 views。")
        return [{"view_id":row.get("id",row.get("view_id")),
                 "view_name":row.get("name",row.get("view_name")),
                 "view_type":row.get("type",row.get("view_type")),**row} for row in rows]

    def create_view(self, table: str, name: str, kind: str):
        data=self.cli.call(["base","+view-create","--base-token",self.token,
                            "--table-id",self.tables[table],"--json",
                            canonical({"name":name,"type":kind})],mutation=True)
        if not isinstance(data,dict):
            raise UnknownWrite("typed view-create 返回结构不兼容，必须核对远端。")
        row=data.get("view") if isinstance(data.get("view"),dict) else data
        view_id=row.get("id",row.get("view_id")) if isinstance(row,dict) else None
        return {**data,"view_id":view_id,"view_name":name,"view_type":kind} if view_id else data

    def _view_call(self, command: str, table: str, view_id: str, payload: dict):
        return self.cli.call(["base",command,"--base-token",self.token,"--table-id",self.tables[table],
                              "--view-id",safe_id(view_id),"--json",canonical(payload)],mutation=True)

    def set_view_visible_fields(self, table: str, view_id: str, visible: list[str]):
        return self._view_call("+view-set-visible-fields",table,view_id,{"visible_fields":visible})

    def set_view_card(self, table: str, view_id: str, cover: str | None):
        return self._view_call("+view-set-card",table,view_id,{"cover_field":cover})

    def set_view_group(self, table: str, view_id: str, group_config: list[dict]):
        return self._view_call("+view-set-group",table,view_id,{"group_config":group_config})

    def set_view_sort(self, table: str, view_id: str, sort_config: list[dict]):
        return self._view_call("+view-set-sort",table,view_id,{"sort_config":sort_config})

    def upload_attachment(self, table: str, record: str, field_name: str, source: Path):
        matches=[f for f in self.list_fields(table) if f.get("field_name")==field_name and f.get("type")==17]
        if len(matches)!=1:
            raise FactoryError("附件字段不存在或类型不符。")
        source=Path(source)
        if source.is_symlink():
            raise FactoryError("附件源文件不存在、不是普通文件或是符号链接；未发起远端写入。")
        source=source.resolve()
        if not source.is_file():
            raise FactoryError("附件源文件不存在、不是普通文件或是符号链接；未发起远端写入。")

        def find():
            rows=self.list_records(table,field_names=[field_name])
            row=next((item for item in rows if item.get("record_id")==record),None)
            if row is None:
                raise FactoryError("附件目标记录不存在。")
            found=[item for item in (row.get("fields",{}).get(field_name) or [])
                   if item.get("name")==source.name and item.get("size")==source.stat().st_size]
            if len(found)>1:
                raise FactoryError("附件字段存在多个同名同大小文件，停止自动归并。")
            return found[0] if found else None

        existing=find()
        if existing is not None:
            return existing
        self.last_attachment_upload_diagnostic=None
        args=["base","+record-upload-attachment","--base-token",self.token,
              "--table-id",self.tables[table],"--record-id",safe_id(record),
              "--field-id",matches[0]["field_id"],"--file",str(source)]
        failure=None
        try:
            caller=getattr(self.cli,"call_attachment",self.cli.call)
            caller(args,mutation=True)
        except UnknownWrite as exc:
            failure=exc
        finally:
            self.last_attachment_upload_diagnostic=getattr(self.cli,"last_diagnostic",None)
        for delay in self.attachment_readback_delays:
            if delay:
                time.sleep(delay)
            confirmed=find()
            if confirmed is not None:
                return confirmed
        if failure is not None:
            raise failure
        raise UnknownWrite("附件上传命令返回成功，但有限读回窗口内未确认字段绑定；禁止重复上传。")

    def download_attachment(self, table: str, record: str, attachment: dict, destination: Path):
        token=safe_id(attachment.get("file_token",attachment.get("token","")))
        destination.parent.mkdir(parents=True,exist_ok=True)
        if destination.exists():destination.unlink()
        self.cli.call(["base","+record-download-attachment","--base-token",self.token,
                       "--table-id",self.tables[table],"--record-id",safe_id(record),
                       "--file-token",token,"--output",str(destination.resolve()),"--overwrite"])
        if not destination.is_file():
            raise FactoryError("下载命令未在指定位置产生文件，不能当作下载成功。")

    # Compatibility names for the offline fixture and older callers. They still
    # route through the Gateway; no business code uses raw records endpoints.
    def list(self, table: str): return self.list_records(table)
    def get(self, table: str, record_id: str): return self.get_record(table,record_id)
    def create(self, table: str, fields: dict): return self.create_record(table,fields)
    def patch(self, table: str, record_id: str, fields: dict): return self.update_record(table,record_id,fields)
    def fields(self, table: str): return self.list_fields(table)
    def upload(self, table: str, record: str, field_name: str, source: Path): return self.upload_attachment(table,record,field_name,source)
    def download(self, table: str, record: str, attachment: dict, destination: Path): return self.download_attachment(table,record,attachment,destination)


FeishuBase = FeishuGateway
