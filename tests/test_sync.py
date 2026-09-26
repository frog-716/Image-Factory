import copy,json,tempfile,unittest
from pathlib import Path
from support import Fixture
from factory.sync import import_metrics,metric_summary,bootstrap,schema_check,schema_plan,apply_schema_additions,reconcile_schema_invalid_parameter,probe_feishu,probe_attachments,seed_workflows
from factory.lark import FeishuBase
from factory.state import State
from factory.schema import TABLES
from factory.util import FactoryError,write_json,read_json
from factory.metrics import COLUMNS

class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.f=Fixture(self.temp.name);self.f.prepare();self.f.all_finished();self.f.placement()
        self.csv=Path(self.temp.name)/'metrics.csv';self.write()
    def write(self,clicks=30,kind='demo',channel='TEST'):
        self.csv.write_text(','.join(COLUMNS)+'\n'+f'PLACE-001,2026-09-02,Asia/Shanghai,{channel},1000,{clicks},2,orders_per_click,{kind}\n')
    def tearDown(self):self.f.close();self.temp.cleanup()
    def test_reimport_does_not_duplicate_metrics(self):
        import_metrics(self.f.base,self.f.state,self.csv);import_metrics(self.f.base,self.f.state,self.csv)
        self.assertEqual(len(self.f.base.list('metrics')),1)
    def test_corrected_revision_preserves_previous(self):
        import_metrics(self.f.base,self.f.state,self.csv);self.write(40)
        import_metrics(self.f.base,self.f.state,self.csv,2,True)
        self.assertEqual(len(self.f.base.list('metrics')),2)
        summary=metric_summary(self.f.base,include_demo=True);self.assertEqual(len(summary),1);self.assertEqual(summary[0]['fields']['点击'],40)
        self.assertTrue(self.f.base.list('metrics')[0]['fields']['来源文件'])
    def test_changed_counts_need_explicit_authorization(self):
        import_metrics(self.f.base,self.f.state,self.csv);self.write(40)
        with self.assertRaises(FactoryError):import_metrics(self.f.base,self.f.state,self.csv)
    def test_wrong_channel_rejected(self):
        self.write(channel='OTHER')
        with self.assertRaises(FactoryError):import_metrics(self.f.base,self.f.state,self.csv)
    def test_missing_placement_rejected(self):
        self.csv.write_text(self.csv.read_text().replace('PLACE-001','MISSING'))
        with self.assertRaises(FactoryError):import_metrics(self.f.base,self.f.state,self.csv)
    def test_live_rejects_mock_metrics(self):
        self.f.base.is_mock=False
        with self.assertRaises(FactoryError):import_metrics(self.f.base,self.f.state,self.csv)
    def test_target_image_hash_mismatch(self):
        p=self.f.base.list('placements')[0];self.f.base.patch('placements',p['record_id'],{'目标SHA256':'changed'})
        with self.assertRaises(FactoryError):import_metrics(self.f.base,self.f.state,self.csv)
    def test_batch_validation_before_any_write(self):
        self.csv.write_text(self.csv.read_text()+'MISSING,2026-09-02,UTC,TEST,1000,30,2,test,demo\n')
        with self.assertRaises(FactoryError):import_metrics(self.f.base,self.f.state,self.csv)
        self.assertEqual(self.f.base.list('metrics'),[])
    def test_duplicate_metric_version_fails_summary(self):
        import_metrics(self.f.base,self.f.state,self.csv);row=self.f.base.list('metrics')[0]
        self.f.base.create('metrics',row['fields'])
        with self.assertRaises(FactoryError):metric_summary(self.f.base,include_demo=True)
    def test_attachment_probe_roundtrip(self):
        result=probe_attachments(self.f.base,self.f.state);self.assertTrue(result['sha256_equal']);self.assertFalse(result['is_live'])

    def test_feishu_probe_crud_leaves_no_probe_record(self):
        result=probe_feishu(self.f.base,self.f.state)
        self.assertTrue(result['record_crud'])
        self.assertFalse(self.f.base.find_unique('assets','资产ID',result['probe_marker']))

class FakeSchemaTransport:
    """A contract fixture, not evidence that the real server accepted these requests."""
    def __init__(self):self.tables=[];self.fields={};self.writes=[]
    def call(self,args,mutation=False):
        self.writes.append((copy.deepcopy(args),mutation))
        command=args[1]
        def value(flag):return args[args.index(flag)+1]
        type_names={1:'text',2:'number',3:'select',7:'checkbox',17:'attachment',18:'link'}
        type_numbers={name:number for number,name in type_names.items()}
        def typed_field(field):
            type_name=type_names.get(field['type'],field['type'])
            result={'id':field['field_id'],'name':field['field_name'],'type':type_name}
            prop=field.get('property',{})
            if type_name=='select':result['options']=copy.deepcopy(prop.get('options',[]))
            if type_name=='link':result['link_table']=prop.get('table_id');result['multiple']=prop.get('multiple',False)
            return result
        def raw_field(field):
            type_number=type_numbers.get(field['type'],field['type'])
            result={'field_name':field['name'],'type':type_number,'field_id':field.get('field_id','')}
            if field['type']=='select':result['property']={'options':copy.deepcopy(field.get('options',[]))}
            if field['type']=='link':result['property']={'table_id':field['link_table'],'multiple':field.get('multiple',False)}
            return result
        if command=='+table-list':
            return {'tables':[{'id':row['table_id'],'name':row['name']} for row in self.tables]}
        if command=='+field-list':
            tid=value('--table-id')
            return {'fields':[typed_field(field) for field in self.fields[tid]]}
        if command=='+table-create':
            tid='tbl_'+str(len(self.tables)+1);name=value('--name')
            specs=json.loads(value('--fields'))
            self.tables.append({'name':name,'table_id':tid})
            self.fields[tid]=[]
            for index,spec in enumerate(specs):
                raw=raw_field(spec);raw['field_id']='fld_primary' if index==0 else f'fld_{index+1}'
                self.fields[tid].append(raw)
            return {'table_id':tid}
        if command=='+field-create':
            tid=value('--table-id');spec=json.loads(value('--json'))
            if isinstance(spec,list):spec=spec[0]
            raw=raw_field(spec);raw['field_id']=f'fld_{len(self.fields[tid])+1}'
            self.fields[tid].append(raw);return {'created':[{'id':raw['field_id']}]}
        raise AssertionError(args)

class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.s=State(self.root/'state')
        self.config=self.root/'config.json';write_json(self.config,{'base_token':'BASE_TEST','tables':{}})
        self.transport=FakeSchemaTransport();self.base=FeishuBase(read_json(self.config),self.transport)
    def tearDown(self):self.s.close();self.temp.cleanup()
    def test_dry_run_has_no_writes(self):
        result=bootstrap(self.base,self.s,self.config,False);self.assertEqual(len(result['tables']),8);self.assertEqual(self.transport.writes,[])
    def test_bootstrap_and_schema_contract(self):
        bootstrap(self.base,self.s,self.config,True);self.assertEqual(schema_check(self.base)['tables_checked'],8)
        self.assertEqual(len(read_json(self.config)['tables']),8)
    def test_repeated_bootstrap_does_not_duplicate(self):
        bootstrap(self.base,self.s,self.config,True);before=len(self.transport.writes)
        bootstrap(self.base,self.s,self.config,True);self.assertEqual(len(self.transport.writes),before)
    def test_schema_type_drift_rejected(self):
        bootstrap(self.base,self.s,self.config,True);self.transport.fields[self.base.tables['products']][0]['type']=2
        with self.assertRaises(FactoryError):schema_check(self.base)
    def test_wrong_link_table_rejected(self):
        bootstrap(self.base,self.s,self.config,True)
        for f in self.transport.fields[self.base.tables['tasks']]:
            if f['field_name']=='商品':f['property']['table_id']='tbl_WRONG'
        with self.assertRaises(FactoryError):schema_check(self.base)
    def test_schema_plan_is_read_only_and_lists_exact_additions(self):
        bootstrap(self.base,self.s,self.config,True)
        table_id=self.base.tables['reviews']
        self.transport.fields[table_id]=[
            field for field in self.transport.fields[table_id]
            if field['field_name']!='Demo视觉检查'
        ]
        before=len(self.transport.writes)
        plan=schema_plan(self.base)
        self.assertEqual(len(self.transport.writes),before+8)
        self.assertIn('reviews.Demo视觉检查', [item['key'] for item in plan['additions']])
        self.assertEqual(plan['drift'],[])
    def test_v1_schema_apply_only_adds_planned_fields_and_checkpoints(self):
        bootstrap(self.base,self.s,self.config,True)
        table_id=self.base.tables['reviews']
        self.transport.fields[table_id]=[
            field for field in self.transport.fields[table_id]
            if field['field_name']!='Demo视觉检查'
        ]
        before_tables=len(self.transport.tables)
        result=apply_schema_additions(self.base,self.s)
        self.assertEqual(result['applied'],['reviews.Demo视觉检查'])
        self.assertEqual(len(self.transport.tables),before_tables)
        self.assertEqual(schema_plan(self.base)['additions'],[])
        checkpoint=self.s.root/'schema-migrations'/'v1-latest.json'
        self.assertTrue(checkpoint.is_file())
        self.assertEqual(read_json(checkpoint)['state'],'complete')

    def test_link_create_payload_omits_unsupported_multiple(self):
        bootstrap(self.base,self.s,self.config,True)
        payload=self.base._typed_field({'field_name':'选用提示词组件','type':18,
                                        'target':'steps','multiple':True})
        self.assertEqual(payload,{'name':'选用提示词组件','type':'link',
                                  'link_table':self.base.tables['steps']})

    def test_reconcile_known_invalid_parameter_only_when_field_is_absent(self):
        bootstrap(self.base,self.s,self.config,True)
        task_table=self.base.tables['tasks']
        self.transport.fields[task_table]=[field for field in self.transport.fields[task_table]
                                           if field['field_name']!='选用提示词组件']
        effect=f'v1-schema:{self.base.token}:tasks.选用提示词组件'
        with self.assertRaises(RuntimeError):
            self.s.effect(effect,lambda:None,lambda:(_ for _ in ()).throw(RuntimeError('invalid')))
        with self.assertRaises(FactoryError):
            reconcile_schema_invalid_parameter(self.base,self.s,'tasks.选用提示词组件','wrong')
        result=reconcile_schema_invalid_parameter(self.base,self.s,'tasks.选用提示词组件','800010701')
        self.assertEqual(result['retry_authorized'],effect)
        self.assertNotIn(effect,self.s.pending_effects())

class MissingTableResponseTests(unittest.TestCase):
    def test_bad_table_response_remains_unknown(self):
        from unittest.mock import patch
        from factory.util import UnknownWrite
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);cfg=root/'config.json';write_json(cfg,{'base_token':'TEST','tables':{}})
            s=State(root/'state');transport=FakeSchemaTransport();base=FeishuBase(read_json(cfg),transport)
            try:
                with patch.object(transport,'call',side_effect=[{'tables':[]},{}]):
                    with self.assertRaises(UnknownWrite):bootstrap(base,s,cfg,True)
                self.assertEqual(len(s.pending_effects()),1)
            finally:s.close()


class TypedRecordLookupTests(unittest.TestCase):
    def test_live_lookup_uses_typed_record_list_matrix(self):
        class Transport:
            def __init__(self):
                self.calls=[]
            def call(self,args,mutation=False):
                self.calls.append((args,mutation))
                return {
                    'data':[['[IF] 背景合成 v1.0']],
                    'fields':['流程名'],
                    'record_id_list':['rec_flow'],
                }

        transport=Transport()
        base=FeishuBase({'base_token':'BASE_TEST','tables':{'workflows':'tbl_flow'}},transport)
        row=base.find_unique_typed('workflows','流程名','[IF] 背景合成 v1.0',field_names=['流程名'])
        self.assertEqual(row['record_id'],'rec_flow')
        self.assertEqual(row['fields']['流程名'],'[IF] 背景合成 v1.0')
        self.assertIn('+record-list',transport.calls[0][0])
        self.assertNotIn('/records', ' '.join(transport.calls[0][0]))

    def test_system_field_types_are_preserved_for_schema_contract(self):
        base=FeishuBase({'base_token':'BASE_TEST','tables':{'reviews':'tbl_reviews'}},object())
        self.assertEqual(base._normalize_field({'id':'fld_at','name':'创建时间','type':'created_at'})['type'],1001)
        self.assertEqual(base._normalize_field({'id':'fld_by','name':'创建人','type':'created_by'})['type'],1003)

    def test_typed_record_list_paginates_by_offset(self):
        class Transport:
            def __init__(self):self.calls=[]
            def call(self,args,mutation=False):
                self.calls.append(args)
                return ({'data':[['A']],'fields':['流程名'],'record_id_list':['rec_a'],'has_more':True}
                        if '--offset' in args and args[args.index('--offset')+1]=='0'
                        else {'data':[['B']],'fields':['流程名'],'record_id_list':['rec_b'],'has_more':False})
        transport=Transport();base=FeishuBase({'base_token':'BASE_TEST','tables':{'workflows':'tbl_flow'}},transport)
        self.assertEqual([row['record_id'] for row in base.list_records('workflows',['流程名'],limit=1)],['rec_a','rec_b'])
        self.assertEqual(transport.calls[1][transport.calls[1].index('--offset')+1],'1')


class WorkflowSeedTests(unittest.TestCase):
    def test_seed_uses_typed_reconcile_and_rejects_conflict(self):
        class Base:
            token='BASE_TEST'
            def __init__(self):
                self.rows={'workflows':[],'steps':[]}
                self.lookup_calls=[]
                self.creates=[]
            def find_unique_typed(self,table,field,value,field_names=None):
                self.lookup_calls.append((table,field,value,tuple(field_names or [])))
                rows=[r for r in self.rows[table] if r['fields'].get(field)==value]
                if len(rows)>1: raise FactoryError('duplicate')
                return copy.deepcopy(rows[0]) if rows else None
            def create(self,table,fields):
                rid=f'rec_{table}_{len(self.rows[table])+1}'
                stored=copy.deepcopy(fields)
                if table=='steps' and stored.get('提示词模板')=='':
                    stored['提示词模板']=None
                row={'record_id':rid,'fields':stored}
                self.rows[table].append(row);self.creates.append((table,copy.deepcopy(fields)))
                return copy.deepcopy(row)
            def create_record(self,table,fields): return self.create(table,fields)

        with tempfile.TemporaryDirectory() as d:
            base=Base();state=State(Path(d)/'state')
            try:
                template=Path(__file__).resolve().parents[1]/'templates/workflow-background.json'
                first=seed_workflows(base,state,template)
                self.assertEqual(first['record_id'],'rec_workflows_1')
                self.assertEqual(len(base.creates),7)
                creates=len(base.creates)
                seed_workflows(base,state,template)
                self.assertEqual(len(base.creates),creates)
                self.assertTrue(any(x[0]=='workflows' for x in base.lookup_calls))
                base.rows['workflows'][0]['fields']['版本']='conflict'
                with self.assertRaises(FactoryError):seed_workflows(base,state,template)
            finally:
                state.close()
