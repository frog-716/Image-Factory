from pathlib import Path
import shutil
from factory.demo import sample_images
from factory.engine import Engine
from factory.mock import MockBase
from factory.state import State
from factory.sync import seed_workflows
from factory.util import file_hash, now, write_json
ROOT=Path(__file__).resolve().parents[1]

class Fixture:
    def __init__(self,root,count=2,calls=3):
        self.root=Path(root);self.fixture=self.root/'fixtures';sample_images(self.fixture)
        self.base=MockBase(self.root/'remote');self.state=State(self.root/'state')
        self.config={'max_images_per_task':12,'max_calls_per_task':18,'reviewer_open_ids':['ou_demo']}
        self.product=self.base.create('products',{'商品名':'测试鞋','SKU':'TEST-001','商品品牌':'TEST',
            '事实版本':'1','已确认事实':'测试素材','禁止改变':'禁止上架','事实来源':'fixture',
            '资料已确认':True,'示例数据':True})['record_id']
        self.asset=self.base.create('assets',{'素材名':'原图','资产ID':'TEST-A01','类型':'商品原图',
            '商品':[self.product],'来源说明':'fixture','授权说明':'测试用','允许用于生图':True,
            '示例数据':True,'SHA256':file_hash(self.fixture/'demo-product.png')})['record_id']
        self.base.upload('assets',self.asset,'图片',self.fixture/'demo-product.png')
        self.base.patch('products',self.product,{'默认商品素材':[self.asset]})
        self.flow=seed_workflows(self.base,self.state,ROOT/'templates/workflow-background.json')['record_id']
        self.task=self.base.create('tasks',{'任务名':'测试任务','商品':[self.product],'流程':[self.flow],
            '渠道':'TEST','图片用途':'广告图','消费场景':'室内','版位':'卡片','视觉风格':'干净',
            '数量':count,'最多调用次数':calls,'提交':True,'取消':False,'示例数据':True})['record_id']
        self.engine=Engine(self.base,self.state,self.config)
    def prepare(self):
        self.run=self.engine.prepare(self.task);self.rid=self.run['id'];return self.run
    def receipt(self,sid='001',start=True,image_index=None):
        dispatch=self.engine.start(self.rid,sid) if start else self.engine.request(self.state.run(self.rid),sid)
        run=self.state.run(self.rid);root=self.engine.run_root(self.rid);incoming=root/'incoming';incoming.mkdir(exist_ok=True)
        attempt=run['slots'][sid]['attempts'][-1]['id']
        shutil.copyfile(self.fixture/f'demo-background-{image_index or int(sid)}.png',incoming/(sid+'.png'))
        (incoming/(sid+'.txt')).write_text('MOCK ONLY; no provider call; test evidence.\n')
        receipt={'run_id':self.rid,'slot':sid,'attempt_id':attempt,'producer':'mock','actual_model':'mock',
            'provider_call_id':'MOCK-'+sid,'generated_at':now(),'prompt_sha256':dispatch['prompt_sha256'],
            'input_sha256':[x['sha256'] for x in dispatch['reference_images']],
            'image_path':'incoming/'+sid+'.png','tool_evidence_path':'incoming/'+sid+'.txt'}
        p=incoming/(sid+'.json');write_json(p,receipt);return p,receipt
    def finish(self,sid='001'):
        p,_=self.receipt(sid);return self.engine.receive(self.rid,sid,p)
    def all_finished(self):
        for sid in self.state.run(self.rid)['slots']:self.finish(sid)
        self.engine.push(self.rid)
    def review(self,sid='001',choice='通过',timestamp=1000,**overrides):
        s=self.state.run(self.rid)['slots'][sid]
        f={'审核标题':'人工测试','图片资产':[s['asset_record_id']],'目标SHA256':s['final_sha256'],
           '结论':choice,'审核人':[{'id':'ou_demo'}],'创建人':[{'id':'ou_demo'}],
           '创建时间':timestamp,'商品准确':True,'品牌及渠道检查':True,'人工确认':True,'原因':'MOCK'}
        f.update(overrides);return self.base.create('reviews',f)
    def placement(self):
        s=self.state.run(self.rid)['slots']['001']
        return self.base.create('placements',{'使用记录名':'测试使用','使用ID':'PLACE-001',
            '图片资产':[s['asset_record_id']],'目标SHA256':s['final_sha256'],'渠道':'TEST',
            '人工确认已上线':True,'上线时间':'2026-09-01T00:00:00+08:00'})
    def close(self):self.state.close()
