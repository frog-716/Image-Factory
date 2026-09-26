import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from ifkit_ref.contracts import (Blocked,Conflict,ContractError,checked_plan,canonical,digest,identifier,sha)
from ifkit_ref.runtime import Runtime
from ifkit_ref.review import validate_review_snapshot,latest_reviews
from ifkit_ref.delivery import build_zip,verify_zip
from ifkit_ref.metrics import MetricBook,normalize
from ifkit_ref.prompts import background
from ifkit_ref.ports import UnconfiguredNativeWorker,CapabilityUnavailable
from ifkit_ref.fixtures import small_plan,mock_receipt,approval_snapshot,metric_fixture
from ifkit_ref.demo import run_demo

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.r=Runtime(Path(self.tmp.name)/'state.sqlite3')
        self.p=small_plan()
        self.r.create(self.p)
        self.auth={'run_id':self.p['run_id'],'plan_hash':digest(self.p),'approved':True,
                   'actor_id':'test-human','max_image_calls':4}
    def tearDown(self):
        self.r.close()
        self.tmp.cleanup()
    def dispatch(self):
        return self.r.dispatch(self.p['run_id'],'bg-a',authorization=self.auth,origin='mock')

class PlanTests(Base):
    def test_recreate_is_idempotent(self):
        self.assertEqual(self.r.create(self.p),'existing')
    def test_mutation_conflicts(self):
        self.p['max_image_calls']=5
        with self.assertRaises(Conflict): self.r.create(self.p)
    def test_only_shoes(self):
        self.p['category']='office_chairs'
        with self.assertRaises(ContractError): checked_plan(self.p)
    def test_bool_budget_rejected(self):
        self.p['max_image_calls']=True
        with self.assertRaises(ContractError): checked_plan(self.p)
    def test_duplicate_step_rejected(self):
        self.p['steps'].append(self.p['steps'][0])
        with self.assertRaises(ContractError): checked_plan(self.p)
    def test_cycle_rejected(self):
        self.p['steps'][0]['depends_on']=['export']
        with self.assertRaises(ContractError): checked_plan(self.p)
    def test_untrusted_command_node_rejected(self):
        self.p['steps'][0]['kind']='shell'
        with self.assertRaises(ContractError): checked_plan(self.p)
    def test_path_traversal_rejected(self):
        with self.assertRaises(ContractError): identifier('../secret')
    def test_unscoped_mode_rejected(self):
        self.p['mode']=''
        with self.assertRaises(ContractError): checked_plan(self.p)
    def test_canonical_is_order_invariant(self):
        self.assertEqual(digest({'a':1,'b':2}),digest({'b':2,'a':1}))

class RuntimeTests(Base):
    def test_no_authorization_no_dispatch(self):
        self.auth['approved']=False
        with self.assertRaises(Blocked): self.dispatch()
    def test_authorization_wrong_plan_rejected(self):
        self.auth['plan_hash']='b'*64
        with self.assertRaises(Blocked): self.dispatch()
    def test_budget_authorization_mismatch(self):
        self.auth['max_image_calls']=5
        with self.assertRaises(Blocked): self.dispatch()
    def test_duplicate_dispatch_blocked(self):
        self.dispatch()
        with self.assertRaises(Blocked): self.dispatch()
    def test_worker_does_not_finish_whole_run(self):
        j=self.dispatch(); rc,files=mock_receipt(j)
        self.r.accept(j['job_id'],rc,files)
        self.assertEqual(self.r.next(self.p['run_id'])['step'],'compose')
    def test_restart_waits_same_job(self):
        j=self.dispatch(); self.r.close(); self.r=Runtime(Path(self.tmp.name)/'state.sqlite3')
        self.assertEqual(self.r.next(self.p['run_id'])['state'],'waiting_worker')
        self.assertEqual(self.r.job(j['job_id'])['state'],'waiting')
    def test_second_connection_cannot_double_claim(self):
        self.dispatch(); other=Runtime(Path(self.tmp.name)/'state.sqlite3')
        try:
            with self.assertRaises(Blocked): other.dispatch(self.p['run_id'],'bg-a',authorization=self.auth,origin='mock')
        finally: other.close()
    def test_unknown_does_not_retry(self):
        j=self.dispatch(); self.r.mark_unknown(j['job_id'],'timeout.log')
        with self.assertRaises(Blocked): self.dispatch()
    def test_empty_get_not_terminal_proof(self):
        j=self.dispatch(); self.r.mark_unknown(j['job_id'],'empty GET')
        with self.assertRaises(Blocked): self.r.prove_no_effect(j['job_id'],worker_terminal=False,no_effect_verified=True,evidence='GET')
    def test_retry_consumes_budget_not_refunded(self):
        j=self.dispatch(); self.r.mark_unknown(j['job_id'],'timeout')
        self.r.prove_no_effect(j['job_id'],worker_terminal=True,no_effect_verified=True,evidence='definitive adapter reconciliation')
        j2=self.dispatch()
        self.assertNotEqual(j['job_id'],j2['job_id'])
        self.assertEqual(self.r.status(self.p['run_id'])['image_calls_reserved'],2)
    def test_budget_exhausted(self):
        for i in range(4):
            j=self.dispatch(); self.r.mark_unknown(j['job_id'],'timeout')
            self.r.prove_no_effect(j['job_id'],worker_terminal=True,no_effect_verified=True,evidence='confirmed terminal')
        with self.assertRaises(Blocked): self.dispatch()
    def test_late_success_accepted_without_resend(self):
        j=self.dispatch(); rc,files=mock_receipt(j)
        self.r.mark_unknown(j['job_id'],'timeout')
        self.r.accept(j['job_id'],rc,files)
        self.assertEqual(self.r.status(self.p['run_id'])['image_calls_reserved'],1)
    def test_receipt_idempotent(self):
        j=self.dispatch(); rc,files=mock_receipt(j)
        self.r.accept(j['job_id'],rc,files)
        self.assertEqual(self.r.accept(j['job_id'],rc,files),'existing')
    def test_wrong_request_hash(self):
        j=self.dispatch(); rc,files=mock_receipt(j); rc['request_hash']='bad'
        with self.assertRaises(Conflict): self.r.accept(j['job_id'],rc,files)
    def test_file_content_tampered(self):
        j=self.dispatch(); rc,files=mock_receipt(j); files['background.png']=b'changed'
        with self.assertRaises(Conflict): self.r.accept(j['job_id'],rc,files)
    def test_mock_cannot_claim_native(self):
        j=self.dispatch(); rc,files=mock_receipt(j); rc['origin']='native'
        with self.assertRaises(Blocked): self.r.accept(j['job_id'],rc,files)
    def test_production_rejects_mock(self):
        p=small_plan('REAL-001',mode='production'); self.r.create(p)
        auth={**self.auth,'run_id':p['run_id'],'plan_hash':digest(p)}
        with self.assertRaises(Blocked): self.r.dispatch(p['run_id'],'bg-a',authorization=auth,origin='mock')
    def test_local_cannot_fake_worker_completion(self):
        with self.assertRaises(Blocked): self.r.finish_local(self.p['run_id'],'bg-a',{'done':True})
    def test_future_step_blocked(self):
        with self.assertRaises(Blocked): self.r.finish_local(self.p['run_id'],'export',{})
    def test_no_native_adapter_fails_explicitly(self):
        with self.assertRaises(CapabilityUnavailable): UnconfiguredNativeWorker().submit({})

class WriteTests(Base):
    def test_journal_before_write(self):
        self.assertEqual(self.r.begin_write('op1',{'x':1}),'reserved')
    def test_pending_not_resubmitted(self):
        self.r.begin_write('op1',{'x':1})
        with self.assertRaises(Blocked): self.r.begin_write('op1',{'x':1})
    def test_unknown_write_not_resubmitted(self):
        self.r.begin_write('op1',{'x':1}); self.r.unknown_write('op1')
        with self.assertRaises(Blocked): self.r.begin_write('op1',{'x':1})
    def test_payload_conflict(self):
        self.r.begin_write('op1',{'x':1})
        with self.assertRaises(Conflict): self.r.begin_write('op1',{'x':2})
    def test_readback_must_match_original(self):
        self.r.begin_write('op1',{'x':1})
        with self.assertRaises(Conflict): self.r.confirm_write('op1',expected_hash='x',observed_hash='x',receipt={'id':'rec1'})
    def test_confirmed_write_skipped(self):
        p={'x':1}; self.r.begin_write('op1',p)
        self.r.confirm_write('op1',expected_hash=digest(p),observed_hash=digest(p),receipt={'id':'rec1'})
        self.assertEqual(self.r.begin_write('op1',p),'existing')

class ReviewTests(unittest.TestCase):
    def setUp(self): self.s,self.a=approval_snapshot()
    def test_demo_does_not_require_false_product_claims(self):
        v=validate_review_snapshot(self.s,'demo'); self.assertEqual(v['approved'],['ASSET-A','ASSET-B'])
    def test_production_checks_remain_required(self):
        self.s,self.a=approval_snapshot(mode='production')
        with self.assertRaises(Blocked): validate_review_snapshot(self.s,'production')
    def test_missing_actor(self):
        self.s['events'][0]['actor_id']=''
        with self.assertRaises(Blocked): validate_review_snapshot(self.s,'demo')
    def test_missing_check(self):
        self.s['events'][0]['demo_use_only']=False
        with self.assertRaises(Blocked): validate_review_snapshot(self.s,'demo')
    def test_pending_does_not_export(self):
        self.s['events'][0]['decision']=''
        with self.assertRaises(Blocked): build_zip(self.s,self.a)
    def test_unreviewed_candidate_blocks(self):
        self.s['events'].pop()
        with self.assertRaises(Blocked): build_zip(self.s,self.a)
    def test_hash_mismatch(self):
        self.s['events'][0]['sha256']='b'*64
        with self.assertRaises(Conflict): build_zip(self.s,self.a)
    def test_revocation_overrides_approval(self):
        event={**self.s['events'][0],'revision':2,'decision':'revoked'}
        self.s['events'].append(event)
        with self.assertRaises(Blocked): build_zip(self.s,self.a)
    def test_revision_conflict(self):
        self.s['events'].append(dict(self.s['events'][0]))
        with self.assertRaises(Conflict): latest_reviews(self.s['events'])
    def test_cross_run_review_rejected(self):
        self.s['events'][0]['run_id']='OTHER'
        with self.assertRaises(Blocked): build_zip(self.s,self.a)
    def test_zip_exact_approved_set(self):
        b,m=build_zip(self.s,self.a)
        with zipfile.ZipFile(io.BytesIO(b)) as z:
            self.assertEqual(set(z.namelist()),{'A.png','B.png','manifest.json','README.txt'})
        verify_zip(b,m)
    def test_zip_reproducible(self):
        b,_=build_zip(self.s,self.a); b2,_=build_zip(self.s,self.a); self.assertEqual(b,b2)
    def test_duplicate_image_rejected(self):
        self.a['ASSET-B']['content']=self.a['ASSET-A']['content']
        h=sha(self.a['ASSET-A']['content'])
        self.s['candidates'][1]['sha256']=h; self.s['events'][1]['sha256']=h
        with self.assertRaises(Conflict): build_zip(self.s,self.a)
    def test_empty_approval_no_fake_zip(self):
        for e in self.s['events']: e['decision']='reject'
        with self.assertRaises(Blocked): build_zip(self.s,self.a)
    def test_file_changed_after_review(self):
        self.a['ASSET-A']['content']=b'changed'
        with self.assertRaises(Conflict): build_zip(self.s,self.a)
    def test_extra_zip_entry_rejected(self):
        b,m=build_zip(self.s,self.a); out=io.BytesIO(b)
        with zipfile.ZipFile(out,'a') as z: z.writestr('secret.txt','bad')
        with self.assertRaises(Conflict): verify_zip(out.getvalue(),m)
    def test_zip_manifest_allowlist(self):
        self.a['ASSET-A']['token']='DO_NOT_EXPORT'
        b,m=build_zip(self.s,self.a)
        self.assertNotIn('token',canonical(m))
        self.assertNotIn('actor_id',canonical(m))

class MetricsTests(unittest.TestCase):
    def setUp(self): self.row,self.usage=metric_fixture()
    def test_numeric_normalization(self):
        self.row['clicks']=40.0
        self.assertEqual(normalize(self.row,self.usage)['clicks'],40)
    def test_zero_denominator_unknown(self):
        self.row['clicks']=0; self.row['impressions']=0
        n=normalize(self.row,self.usage); self.assertIsNone(n['ctr']); self.assertIsNone(n['orders_per_click'])
    def test_missing_count_unknown(self):
        self.row['orders']=None
        self.assertIsNone(normalize(self.row,self.usage)['orders_per_click'])
    def test_fractional_count_rejected(self):
        self.row['orders']=1.2
        with self.assertRaises(ContractError): normalize(self.row,self.usage)
    def test_nonfinite_rejected(self):
        self.row['orders']='NaN'
        with self.assertRaises(ContractError): normalize(self.row,self.usage)
    def test_mode_mixing_rejected(self):
        self.usage['mode']='production'
        with self.assertRaises(Blocked): normalize(self.row,self.usage)
    def test_simulated_in_production_rejected(self):
        self.row['mode']=self.usage['mode']='production'
        with self.assertRaises(Blocked): normalize(self.row,self.usage)
    def test_same_import_idempotent(self):
        b=MetricBook(); b.import_one(self.row,self.usage); self.row['orders']=2.0
        self.assertEqual(b.import_one(self.row,self.usage),'existing')
    def test_same_source_different_value_conflicts(self):
        b=MetricBook(); b.import_one(self.row,self.usage); self.row['orders']=3
        with self.assertRaises(Conflict): b.import_one(self.row,self.usage)
    def test_prod_query_excludes_demo(self):
        b=MetricBook(); b.import_one(self.row,self.usage); self.assertEqual(b.query('production'),[])
    def test_usage_dimensions_must_match(self):
        self.row['placement']='other'
        with self.assertRaises(Conflict): normalize(self.row,self.usage)
    def test_reversed_dates(self):
        self.row['date_from']='2026-10-01'
        with self.assertRaises(ContractError): normalize(self.row,self.usage)

class PromptTests(unittest.TestCase):
    def test_no_reference_files_default(self): self.assertEqual(background('outdoor')['image_references'],[])
    def test_disallowed_image_input_rejected(self):
        with self.assertRaises(Blocked): background('studio',[{'use':'image_reference','allow_image_generation':False}])
    def test_auto_caption_not_permission_bypass(self):
        with self.assertRaises(Blocked): background('indoor',[{'use':'description_reference','description_source':'auto_caption','allow_description_reuse':True}])
    def test_invalid_scene(self):
        with self.assertRaises(ContractError): background('office-chair')
    def test_three_scenes_differ(self):
        self.assertEqual(len({background(x)['prompt_hash'] for x in ('outdoor','indoor','studio')}),3)

class DemoTests(unittest.TestCase):
    def test_full_offline_run(self):
        with tempfile.TemporaryDirectory() as d:
            s=run_demo(Path(d)/'run')
            self.assertEqual(s['runtime']['next']['state'],'completed')
            self.assertEqual(s['zip_image_count'],2)
            self.assertEqual(s['metric_second_import'],'existing')
            self.assertEqual(s['real_image_calls'],0)
    def test_demo_does_not_overwrite_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            run_demo(d)
            with self.assertRaises(ValueError): run_demo(d)

if __name__=='__main__': unittest.main()
