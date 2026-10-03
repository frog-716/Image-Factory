from __future__ import annotations
import copy
import shutil
from pathlib import Path
from .util import FactoryError, read_json, write_json, file_hash, text

class MockBase:
    """Persistent offline fixture. It never impersonates a live Feishu connection."""
    is_mock = True
    token = "DEMO_ONLY"
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.path = self.root/"mock-base.json"
        self.data = read_json(self.path) if self.path.exists() else {}
        self.tables = {x:x for x in self.data}
    def save(self):
        write_json(self.path,self.data)
    def current_user(self):
        return {"open_id": getattr(self, "mock_reviewer_open_id", "ou_demo")}
    def list(self,table):
        return copy.deepcopy(self.data.get(table,[]))
    def get(self,table,record):
        rows = [r for r in self.list(table) if r["record_id"]==record]
        if len(rows)!=1:
            raise FactoryError(f"示例记录不存在：{table}/{record}")
        return rows[0]
    def create(self,table,fields):
        rid = f"rec_{table}_{len(self.data.setdefault(table,[]))+1:05d}"
        row = {"record_id":rid,"fields":copy.deepcopy(fields)}
        self.data[table].append(row);self.save()
        return copy.deepcopy(row)
    def patch(self,table,record,fields):
        for row in self.data.get(table,[]):
            if row["record_id"]==record:
                row["fields"].update(copy.deepcopy(fields));self.save();return copy.deepcopy(row)
        raise FactoryError("示例记录不存在。")
    def find_unique(self,table,field,value):
        rows = [r for r in self.list(table) if text(r["fields"].get(field))==value]
        if len(rows)>1:
            raise FactoryError("存在重复业务ID。")
        return rows[0] if rows else None
    def find_unique_typed(self,table,field,value,field_names=None):
        return self.find_unique(table,field,value)

    def list_records(self,table,field_names=None,filter_json=None,limit=200):
        rows=self.list(table)
        if isinstance(filter_json,dict):
            conditions=filter_json.get('conditions',[])
            for condition in conditions:
                if len(condition)>=3 and condition[1]=='==' and isinstance(condition[0],str):
                    rows=[row for row in rows if row['fields'].get(condition[0])==condition[2]]
        if field_names:
            rows=[{'record_id':row['record_id'],'fields':{name:row['fields'].get(name) for name in field_names}} for row in rows]
        return rows[:limit]

    def get_record(self,table,record_id,field_names=None):
        row=self.get(table,record_id)
        if field_names:
            return {'record_id':row['record_id'],'fields':{name:row['fields'].get(name) for name in field_names}}
        return row

    def create_record(self,table,fields): return self.create(table,fields)
    def update_record(self,table,record_id,fields): return self.patch(table,record_id,fields)
    def delete_record(self,table,record_id):
        rows=self.data.get(table,[])
        before=len(rows);self.data[table]=[row for row in rows if row['record_id']!=record_id]
        if len(self.data[table])==before: raise FactoryError('示例记录不存在。')
        self.save();return {'deleted_record_id':record_id}
    def list_fields(self,table): return []
    def upload_attachment(self,table,record,field_name,source): return self.upload(table,record,field_name,source)
    def download_attachment(self,table,record,attachment,destination): return self.download(table,record,attachment,destination)
    def download(self,table,record,attachment,destination):
        origin=self.root/"attachments"/attachment["file_token"]
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(origin,destination)
    def upload(self,table,record,field,source):
        token="file_"+file_hash(source)
        dest=self.root/"attachments"/token
        dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,dest)
        items=self.get(table,record)["fields"].get(field,[])
        attachment={"file_token":token,"name":source.name,"size":source.stat().st_size}
        self.patch(table,record,{field:items+[attachment]})
        return attachment
