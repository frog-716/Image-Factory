import copy,json,shutil,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from support import Fixture
from factory.engine import Engine,restore_archive
from factory.state import State
from factory.model import sources,compile_prompt
from factory.util import FactoryError,file_hash,write_json,digest

class EngineTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.f=Fixture(self.temp.name)
    def tearDown(self):self.f.close();self.temp.cleanup()
    def test_prepare_idempotent(self):
        r=self.f.prepare();r2=self.f.engine.prepare(self.f.task)
        self.assertEqual(r['id'],r2['id']);self.assertEqual(r2['calls_reserved'],0)
    def test_demo_cannot_enter_live(self):
        with self.assertRaises(FactoryError):sources(self.f.base,self.f.task,12,18,False)
    def test_missing_facts_blocks(self):
        self.f.base.patch('products',self.f.product,{'已确认事实':''})
        with self.assertRaises(FactoryError):self.f.prepare()
    def test_missing_asset_id_blocks(self):
        self.f.base.patch('assets',self.f.asset,{'资产ID':''})
        with self.assertRaises(FactoryError):self.f.prepare()
    def test_missing_rights_blocks(self):
        self.f.base.patch('assets',self.f.asset,{'授权说明':''})
        with self.assertRaises(FactoryError):self.f.prepare()
    def test_wrong_sku_ownership_blocks(self):
        self.f.base.patch('assets',self.f.asset,{'商品':['rec_other']})
        with self.assertRaises(FactoryError):self.f.prepare()
    def test_consumer_scene_cannot_be_missing(self):
        self.f.base.patch('tasks',self.f.task,{'消费场景':''})
        with self.assertRaises(FactoryError):self.f.prepare()
    def test_opaque_product_cannot_use_composite(self):
        from PIL import Image
        path=self.f.fixture/'opaque.png';Image.new('RGB',(64,64)).save(path)
        self.f.base.patch('assets',self.f.asset,{'图片':[],'SHA256':file_hash(path)})
        self.f.base.upload('assets',self.f.asset,'图片',path)
        with self.assertRaises(FactoryError):self.f.prepare()
    def test_invalid_node_order_blocks(self):
        row=self.f.base.list('steps')[0];self.f.base.patch('steps',row['record_id'],{'节点类型':'执行任意代码'})
        with self.assertRaises(FactoryError):self.f.prepare()
    def test_prompt_unknown_variable_blocks(self):
        row=self.f.base.list('steps')[0];self.f.base.patch('steps',row['record_id'],{'提示词模板':'${env.SECRET}'})
        with self.assertRaises(FactoryError):self.f.prepare()
    def test_business_shell_is_data_only(self):
        self.f.base.patch('tasks',self.f.task,{'附加要求':'$(touch /tmp/ATTACK) ${env.SECRET}'})
        r=self.f.prepare();p=compile_prompt(r['source'],1);self.assertIn('$(touch /tmp/ATTACK)',p)
    def test_input_edit_after_prepare_blocks(self):
        self.f.prepare();self.f.base.patch('tasks',self.f.task,{'视觉风格':'改变'})
        with self.assertRaises(FactoryError):self.f.engine.start(self.f.rid,'001')
    def test_local_input_tamper_blocks(self):
        r=self.f.prepare();p=self.f.engine.run_root(self.f.rid)/r['inputs'][0]['path'];p.write_bytes(b'tamper')
        with self.assertRaises(FactoryError):self.f.engine.start(self.f.rid,'001')
    def test_frozen_request_tamper_blocks(self):
        self.f.prepare();p=self.f.engine.run_root(self.f.rid)/'requests/001.json';d=json.loads(p.read_text());d['prompt']='changed';write_json(p,d)
        with self.assertRaises(FactoryError):self.f.engine.start(self.f.rid,'001')
    def test_duplicate_dispatch_blocks(self):
        self.f.prepare();self.f.engine.start(self.f.rid,'001')
        with self.assertRaises(FactoryError):self.f.engine.start(self.f.rid,'001')
        self.assertEqual(self.f.state.run(self.f.rid)['calls_reserved'],1)
    def test_remote_checkpoint_precedes_tool_dispatch(self):
        self.f.prepare();self.f.engine.start(self.f.rid,'001')
        task=self.f.base.get('tasks',self.f.task)['fields'];archive=task['运行档案'][-1]
        path=self.f.base.root/'attachments'/archive['file_token']
        self.assertEqual(task['最新档案SHA256'],file_hash(path))
        with zipfile.ZipFile(path) as z:r=json.loads(z.read('run.json'))
        self.assertEqual(r['slots']['001']['state'],'STARTED');self.assertEqual(r['calls_reserved'],1)
    def test_checkpoint_failure_does_not_reset_slot(self):
        self.f.prepare()
        with patch.object(self.f.engine,'archive',side_effect=FactoryError('offline')):
            with self.assertRaises(FactoryError):self.f.engine.start(self.f.rid,'001')
        self.assertEqual(self.f.state.run(self.f.rid)['slots']['001']['state'],'STARTED')
    def test_retry_keeps_budget(self):
        self.f.prepare();self.f.engine.start(self.f.rid,'001')
        self.f.engine.retry_slot(self.f.rid,'001','quota ambiguity',True);self.f.engine.start(self.f.rid,'001')
        self.assertEqual(self.f.state.run(self.f.rid)['calls_reserved'],2)
    def test_retry_requires_ack(self):
        self.f.prepare();self.f.engine.start(self.f.rid,'001')
        with self.assertRaises(FactoryError):self.f.engine.retry_slot(self.f.rid,'001','reason',False)
    def test_calls_cannot_exceed_budget(self):
        self.f.base.patch('tasks',self.f.task,{'最多调用次数':2});self.f.prepare()
        self.f.engine.start(self.f.rid,'001');self.f.engine.start(self.f.rid,'002')
        with self.assertRaises(FactoryError):self.f.engine.retry_slot(self.f.rid,'001','retry',True)
    def test_old_attempt_receipt_blocks(self):
        self.f.prepare();p,r=self.f.receipt();self.f.engine.retry_slot(self.f.rid,'001','retry',True)
        self.f.engine.start(self.f.rid,'001')
        with self.assertRaises(FactoryError):self.f.engine.receive(self.f.rid,'001',p)
    def test_wrong_prompt_receipt_blocks(self):
        self.f.prepare();p,r=self.f.receipt();r['prompt_sha256']='0'*64;write_json(p,r)
        with self.assertRaises(FactoryError):self.f.engine.receive(self.f.rid,'001',p)
    def test_wrong_actual_references_blocks(self):
        self.f.prepare();p,r=self.f.receipt();r['input_sha256']=['not-used'];write_json(p,r)
        with self.assertRaises(FactoryError):self.f.engine.receive(self.f.rid,'001',p)
    def test_receipt_path_traversal_blocks(self):
        self.f.prepare();p,r=self.f.receipt();r['image_path']='../../secret.png';write_json(p,r)
        with self.assertRaises(FactoryError):self.f.engine.receive(self.f.rid,'001',p)
    def test_paid_api_cannot_impersonate_native(self):
        self.f.prepare();p,r=self.f.receipt();r['producer']='openai_api';write_json(p,r)
        with self.assertRaises(FactoryError):self.f.engine.receive(self.f.rid,'001',p)
    def test_timezone_required(self):
        self.f.prepare();p,r=self.f.receipt();r['generated_at']='2026-09-01T12:00:00';write_json(p,r)
        with self.assertRaises(FactoryError):self.f.engine.receive(self.f.rid,'001',p)
    def test_same_receipt_idempotent(self):
        self.f.prepare();p,r=self.f.receipt();a=self.f.engine.receive(self.f.rid,'001',p);b=self.f.engine.receive(self.f.rid,'001',p)
        self.assertEqual(a['final_sha256'],b['final_sha256'])
    def test_same_image_cannot_fill_two_slots(self):
        self.f.prepare();self.f.finish();p,r=self.f.receipt('002',image_index=1)
        with self.assertRaises(FactoryError):self.f.engine.receive(self.f.rid,'002',p)
    def test_push_idempotent(self):
        self.f.prepare();self.f.all_finished();self.f.engine.push(self.f.rid)
        self.assertEqual(len(self.f.base.list('assets')),3)
    def test_new_machine_must_restore_existing_run(self):
        self.f.prepare();s=State(Path(self.temp.name)/'other')
        try:
            e=Engine(self.f.base,s,self.f.config)
            with self.assertRaises(FactoryError):e.prepare(self.f.task)
        finally:s.close()
    def test_archive_capacity_blocks(self):
        self.f.config['max_archive_bytes']=1
        with self.assertRaises(FactoryError):self.f.prepare()
    def test_cancel_prevents_new_dispatch(self):
        self.f.prepare();self.f.engine.cancel(self.f.rid)
        with self.assertRaises(FactoryError):self.f.engine.start(self.f.rid,'001')
    def test_restore_preserves_uncertain_slot(self):
        self.f.prepare();self.f.engine.start(self.f.rid,'001');path=self.f.engine.archive(self.f.rid)
        s=State(Path(self.temp.name)/'restored')
        try:
            r=restore_archive(path,s,self.f.base.token);e=Engine(self.f.base,s,self.f.config)
            self.assertEqual(r['slots']['001']['state'],'STARTED')
            with self.assertRaises(FactoryError):e.start(r['id'],'001')
        finally:s.close()
    def test_backup_traversal_rejected(self):
        p=Path(self.temp.name)/'bad.zip'
        with zipfile.ZipFile(p,'w') as z:z.writestr('../secret','bad')
        with self.assertRaises(FactoryError):restore_archive(p,self.f.state,self.f.base.token)
    def test_backup_checksum_tamper_rejected(self):
        self.f.prepare();original=self.f.engine.archive(self.f.rid);p=Path(self.temp.name)/'tampered.zip'
        with zipfile.ZipFile(original) as src,zipfile.ZipFile(p,'w') as out:
            for n in src.namelist():out.writestr(n,b'bad' if n=='run.json' else src.read(n))
        with self.assertRaises(FactoryError):restore_archive(p,self.f.state,self.f.base.token)

class ReviewTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.f=Fixture(self.temp.name);self.f.prepare();self.f.all_finished()
    def tearDown(self):self.f.close();self.temp.cleanup()
    def test_wrong_hash_review_blocks(self):
        self.f.review(目标SHA256='wrong')
        with self.assertRaises(FactoryError):self.f.engine.sync_reviews(self.f.rid)
    def test_wrong_system_creator_blocks(self):
        self.f.review(创建人=[{'id':'ou_attacker'}])
        with self.assertRaises(FactoryError):self.f.engine.sync_reviews(self.f.rid)
    def test_missing_human_checkbox_blocks(self):
        self.f.review(人工确认=False)
        with self.assertRaises(FactoryError):self.f.engine.sync_reviews(self.f.rid)
    def test_missing_product_check_blocks(self):
        self.f.review(商品准确=False)
        with self.assertRaises(FactoryError):self.f.engine.sync_reviews(self.f.rid)
    def test_review_edit_detected(self):
        row=self.f.review();self.f.engine.sync_reviews(self.f.rid);self.f.base.patch('reviews',row['record_id'],{'结论':'驳回'})
        with self.assertRaises(FactoryError):self.f.engine.sync_reviews(self.f.rid)
    def test_review_deletion_detected(self):
        self.f.review();self.f.engine.sync_reviews(self.f.rid);self.f.base.data['reviews']=[];self.f.base.save()
        with self.assertRaises(FactoryError):self.f.engine.sync_reviews(self.f.rid)
    def test_revocation_removes_previously_approved(self):
        self.f.review();self.f.review('002');self.f.engine.sync_reviews(self.f.rid)
        self.f.review('001','撤回',2000);path,m=self.f.engine.export(self.f.rid)
        self.assertEqual(m['delivered_count'],1)
    def test_approve_reject_package_counts(self):
        self.f.review();self.f.review('002','驳回');path,m=self.f.engine.export(self.f.rid)
        with zipfile.ZipFile(path) as z:
            images=[n for n in z.namelist() if n.endswith('.png')]
            self.assertEqual(len(images),1);self.assertEqual(m['delivered_count'],1)
            self.assertIn('SHA256SUMS.txt',z.namelist())
    def test_missing_review_needs_explicit_partial(self):
        self.f.review()
        with self.assertRaises(FactoryError):self.f.engine.export(self.f.rid)
        _,m=self.f.engine.export(self.f.rid,True);self.assertTrue(m['partial_authorized'])
    def test_no_approved_images_no_zip(self):
        self.f.review('001','驳回');self.f.review('002','驳回')
        with self.assertRaises(FactoryError):self.f.engine.export(self.f.rid)
    def test_remote_attachment_mutation_blocks_export(self):
        self.f.review();self.f.review('002');run=self.f.state.run(self.f.rid);rid=run['slots']['001']['asset_record_id']
        self.f.base.patch('assets',rid,{'图片':[]});self.f.base.upload('assets',rid,'图片',self.f.fixture/'demo-background-3.png')
        with self.assertRaises(FactoryError):self.f.engine.export(self.f.rid)
    def test_same_timestamp_conflicting_reviews_block(self):
        self.f.review();self.f.review('001','驳回')
        with self.assertRaises(FactoryError):self.f.engine.sync_reviews(self.f.rid)

class CheckpointVisibilityTests(unittest.TestCase):
    def test_upload_success_without_visible_attachment_blocks_dispatch(self):
        with tempfile.TemporaryDirectory() as d:
            f=Fixture(d)
            try:
                f.prepare()
                original=f.base.get
                def invisible(table,record):
                    r=original(table,record)
                    if table=='tasks':r['fields']['运行档案']=[]
                    return r
                with patch.object(f.base,'get',side_effect=invisible):
                    with self.assertRaises(FactoryError):f.engine.start(f.rid,'001')
                self.assertEqual(f.state.run(f.rid)['slots']['001']['state'],'STARTED')
            finally:f.close()
