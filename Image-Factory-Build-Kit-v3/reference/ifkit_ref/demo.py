"""Real local execution with fake worker/approval inputs, intentionally no live integrations."""
import json
from pathlib import Path
from .contracts import digest, sha
from .runtime import Runtime
from .fixtures import small_plan,mock_receipt,approval_snapshot,metric_fixture
from .delivery import build_zip,verify_zip
from .metrics import MetricBook

def run_demo(out: str | Path) -> dict:
    root=Path(out)
    root.mkdir(parents=True,exist_ok=True)
    if any(root.iterdir()):
        raise ValueError('Use a new empty output directory; existing evidence is never overwritten')
    p=small_plan()
    r=Runtime(root/'ledger.sqlite3')
    r.create(p)
    auth={'run_id':p['run_id'],'plan_hash':digest(p),'approved':True,
          'actor_id':'offline-fixture','max_image_calls':p['max_image_calls']}
    job=r.dispatch(p['run_id'],'bg-a',authorization=auth,origin='mock')
    receipt,artifacts=mock_receipt(job)
    (root/'worker-request.json').write_text(json.dumps(job,ensure_ascii=False,indent=2),encoding='utf-8')
    # Simulate app/process restart after dispatch. It must still wait for same job.
    r.close()
    r=Runtime(root/'ledger.sqlite3')
    resumed_state=r.next(p['run_id'])['state']
    r.accept(job['job_id'],receipt,artifacts)
    snapshot,assets=approval_snapshot()
    r.finish_local(p['run_id'],'compose',{'origin':'mock','note':'algorithmic fixtures, no real shoe composition'})
    r.wait_review(p['run_id'],'review')
    r.finish_review(p['run_id'],'review',validated_snapshot=snapshot)
    archive,manifest=build_zip(snapshot,assets)
    (root/'offline-delivery.zip').write_bytes(archive)
    verify_zip((root/'offline-delivery.zip').read_bytes(),manifest)
    r.finish_local(p['run_id'],'export',{'zip_sha256':sha(archive),'image_count':manifest['image_count']})
    row,usage=metric_fixture()
    book=MetricBook()
    first,second=book.import_one(row,usage),book.import_one(row,usage)
    r.finish_local(p['run_id'],'metrics',{'first':first,'second':second,'mode':'demo'})
    summary={'scope':'OFFLINE REFERENCE ONLY','real_image_calls':0,'real_feishu_writes':0,
             'human_review_origin':'mock_fixture_not_human','restart_state':resumed_state,
             'approved_count':2,'rejected_count':1,'zip_image_count':manifest['image_count'],
             'zip_sha256':sha(archive),'metric_second_import':second,
             'production_metric_count':len(book.query('production')),'runtime':r.status(p['run_id'])}
    (root/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    r.close()
    return summary
