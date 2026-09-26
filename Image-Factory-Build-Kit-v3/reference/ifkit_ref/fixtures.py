"""Tiny algorithmic image fixtures, never AI demos or real merchandise."""
import struct
import zlib
from .contracts import sha

def fixture_png(rgb=(100,150,220),size=32):
    def chunk(name,data):
        return struct.pack('!I',len(data))+name+data+struct.pack('!I',zlib.crc32(name+data)&0xffffffff)
    raw=b''.join(b'\x00'+bytes(rgb)*size for _ in range(size))
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',size,size,8,2,0,0,0))+
            chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b''))

def small_plan(run_id='OFFLINE-KIDS-001',mode='demo',max_calls=4):
    return {'schema_version':1,'category':'kids_shoes','run_id':run_id,'mode':mode,
            'workflow_version':'reference-v1','max_image_calls':max_calls,
            'steps':[{'id':'bg-a','kind':'image_generate','prompt':'fixture A','depends_on':[]},
                     {'id':'compose','kind':'compose','depends_on':['bg-a']},
                     {'id':'review','kind':'human_review','depends_on':['compose']},
                     {'id':'export','kind':'export','depends_on':['review']},
                     {'id':'metrics','kind':'metrics','depends_on':['export']}]}

def mock_receipt(job,content=None):
    data=fixture_png() if content is None else content
    return {'job_id':job['job_id'],'request_hash':job['request_hash'],
            'mode':job['request']['mode'],'status':'succeeded','origin':'mock',
            'tool':'offline_fixture','model_reported':'not_applicable','evidence_ref':'offline-fixture',
            'outputs':[{'name':'background.png','sha256':sha(data),'bytes':len(data)}]}, {'background.png':data}

def approval_snapshot(run_id='OFFLINE-KIDS-001',mode='demo'):
    assets={}
    candidates=[]
    events=[]
    for i,letter in enumerate('ABC'):
        data=fixture_png((60+i*45,150,210-i*25))
        aid='ASSET-'+letter
        assets[aid]={'run_id':run_id,'mode':mode,'name':letter+'.png','content':data,'origin':'mock'}
        candidates.append({'asset_id':aid,'sha256':sha(data)})
        events.append({'asset_id':aid,'sha256':sha(data),'run_id':run_id,'mode':mode,
                       'revision':1,'decision':'reject' if letter=='C' else 'approve',
                       'actor_id':'offline-fixture-actor','human_confirmed':True,
                       'reason':'OFFLINE TEST ONLY; no real human approval',
                       'demo_visual_ok':True,'demo_use_only':True,
                       'product_accuracy':False,'brand_channel_ok':False})
    return {'run_id':run_id,'mode':mode,'candidates':candidates,'events':events},assets

def metric_fixture():
    usage={'mode':'demo','usage_id':'OFFLINE-USAGE-A','asset_id':'ASSET-A',
           'channel':'demo','account_id':'demo','placement':'demo-ad'}
    row={**usage,'date_from':'2026-09-01','date_to':'2026-09-07','timezone':'Asia/Shanghai',
         'attribution_window':'demo_7d_click','definition_version':'demo-v1','source_sha256':'a'*64,
         'impressions':1000,'clicks':40,'orders':2,'data_nature':'simulated'}
    return row,usage
