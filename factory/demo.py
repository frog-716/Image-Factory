from __future__ import annotations
import shutil
from pathlib import Path
from PIL import Image, ImageDraw
from .mock import MockBase
from .state import State
from .engine import Engine
from .sync import seed_workflows, import_metrics
from .util import digest, file_hash, now, write_json


def sample_images(directory:Path):
    directory.mkdir(parents=True,exist_ok=True)
    product=Image.new("RGBA",(600,350),(0,0,0,0));d=ImageDraw.Draw(product)
    d.rounded_rectangle((60,150,535,295),radius=50,fill=(230,225,210,255),outline=(30,30,30,255),width=6)
    d.polygon([(110,170),(155,75),(295,115),(365,195)],fill=(230,225,210,255),outline=(30,30,30,255))
    d.line([(85,260),(525,260)],fill=(20,20,20,255),width=8)
    d.text((195,205),"DEMO / NOT A REAL PRODUCT",fill=(20,20,20,255))
    product.save(directory/"demo-product.png")
    for i in range(1,4):
        bg=Image.new("RGB",(1024,1024),(224+i*7,230-i*8,236-i*10));draw=ImageDraw.Draw(bg)
        draw.rectangle((0,650,1024,1024),fill=(210+i*8,211-i*4,208-i*9))
        draw.ellipse((50+i*100,80,320+i*100,350),outline=(130,130,130),width=4)
        draw.text((35,35),f"SIMULATION {i}: NOT AI GENERATED / NOT FOR SALE",fill=(10,10,10))
        bg.save(directory/f"demo-background-{i}.png")


def run_demo(destination:Path,project_root:Path):
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("演练目录已存在且非空，请指定新的 --out；不覆盖旧演练。")
    destination.mkdir(parents=True,exist_ok=True)
    fixture=destination/"fixtures";sample_images(fixture)
    base=MockBase(destination/"feishu-simulator")
    state=State(destination/"runtime")
    config={"max_images_per_task":12,"max_calls_per_task":18,"reviewer_open_ids":["ou_demo"]}
    try:
        product=base.create_record("products",{"商品名":"DEMO 示例鞋形图","SKU":"DEMO-001","商品品牌":"DEMO",
            "事实版本":"demo-v1","已确认事实":"这是程序画的示例轮廓，不对应任何真实商品。",
            "禁止改变":"禁止冒充 TOPSTAR 实拍，禁止真实上架。","事实来源":"本工程演练生成器",
            "资料已确认":True,"示例数据":True})
        asset=base.create_record("assets",{"素材名":"示例透明商品轮廓","资产ID":"DEMO-A01","类型":["商品原图"],
            "商品":[product["record_id"]],"来源说明":"程序绘制 DEMO","授权说明":"仅用于软件流程测试",
            "允许用于生图":True,"示例数据":True,"SHA256":file_hash(fixture/"demo-product.png")})
        base.upload_attachment("assets",asset["record_id"],"图片",fixture/"demo-product.png")
        base.update_record("products",product["record_id"],{"默认商品素材":[asset["record_id"]]})
        workflow=seed_workflows(base,state,project_root/"templates/workflow-background.json")
        task=base.create_record("tasks",{"任务名":"演练：3 张候选、2 张批准","商品":[product["record_id"]],
            "流程":[workflow["record_id"]],"渠道":"DEMO渠道","图片用途":"场景广告图","消费场景":"模拟室内",
            "版位":"模拟广告卡片","视觉风格":"演练用几何背景","附加要求":"禁止上架",
            "变体方向":"左侧光线\n右侧光线\n更宽留白","数量":3,"最多调用次数":4,
            "提交":True,"取消":False,"示例数据":True,"系统状态":"草稿"})
        engine=Engine(base,state,config);run=engine.prepare(task["record_id"])
        for i in range(1,4):
            sid=f"{i:03d}";dispatch=engine.start(run["id"],sid);root=engine.run_root(run["id"])
            incoming=root/"incoming";incoming.mkdir(exist_ok=True)
            shutil.copyfile(fixture/f"demo-background-{i}.png",incoming/(sid+".png"))
            (incoming/(sid+".txt")).write_text("MOCK generator; no external image model or account was called.\n")
            receipt={"run_id":run["id"],"slot":sid,"attempt_id":dispatch["attempt_id"],"producer":"mock",
                "actual_model":"mock_fixture","provider_call_id":"DEMO-"+sid,"generated_at":now(),
                "prompt_sha256":dispatch["prompt_sha256"],"input_sha256":[x["sha256"] for x in dispatch["reference_images"]],
                "image_path":"incoming/"+sid+".png","tool_evidence_path":"incoming/"+sid+".txt"}
            receipt_path=incoming/(sid+".json");write_json(receipt_path,receipt)
            engine.receive(run["id"],sid,receipt_path)
        engine.push(run["id"]);run=state.run(run["id"])
        for i,(sid,slot) in enumerate(run["slots"].items(),1):
            base.create_record("reviews",{"审核标题":"模拟人工审核 "+sid,"图片资产":[slot["asset_record_id"]],
                "目标SHA256":slot["final_sha256"],"结论":"通过" if i<3 else "驳回",
                "审核人":[{"id":"ou_demo"}],"创建人":[{"id":"ou_demo"}],"创建时间":1789785600000+i,
                "商品准确":True,"品牌及渠道检查":True,"人工确认":True,"原因":"DEMO ONLY"})
        engine.sync_reviews(run["id"]);output,manifest=engine.export(run["id"])
        slot=run["slots"]["001"]
        base.create_record("placements",{"使用记录名":"模拟使用","使用ID":"DEMO-PLACEMENT-001",
            "图片资产":[slot["asset_record_id"]],"目标SHA256":slot["final_sha256"],"渠道":"DEMO渠道",
            "店铺或账户":"DEMO","版位":"模拟广告卡片","人工确认已上线":True,
            "上线时间":"2026-09-01T00:00:00+08:00","广告或页面ID":"DEMO-NOT-PUBLISHED"})
        csv=destination/"demo-metrics.csv"
        csv.write_text("placement_id,date,timezone,channel,impressions,clicks,orders,metric_definition,source_kind\n"
            "DEMO-PLACEMENT-001,2026-09-02,Asia/Shanghai,DEMO渠道,1000,30,2,演练_订单数除点击数,demo\n",encoding="utf-8")
        feedback=import_metrics(base,state,csv)
        summary={"mode":"offline_mock","live_feishu_tested":False,"native_image_generation_tested":False,
            "run_id":run["id"],"requested":3,"generated":3,"approved":2,"rejected":1,"delivered":manifest["delivered_count"],
            "metrics_rows":len(feedback),"delivery_zip":str(output.relative_to(destination)),"delivery_sha256":file_hash(output),
            "source_digest":run["source_digest"]}
        write_json(destination/"demo-summary.json",summary)
        # This HTML is an offline evidence viewer, not a replacement product UI.
        cards=[]
        for sid,s in run["slots"].items():
            image=engine.run_root(run["id"])/s["final_path"]
            rel=str(image.relative_to(destination))
            cards.append(f'<article><img src="{rel}" alt="模拟候选 {sid}"><h3>候选 {sid}</h3><p>{"模拟批准" if sid!="003" else "模拟驳回"}</p></article>')
        html='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>离线流程演练结果</title><style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:20px;background:#f5f5f5;color:#222}h1{font-size:28px}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:20px}article{background:white;padding:16px;border-radius:12px}img{width:100%}.note{padding:16px;border:2px solid #aaa;margin:24px 0}</style>
<h1>流程演练：3 张候选 → 2 张批准 → ZIP → 模拟反馈</h1><p class="note">所有图片均为程序绘制的测试素材，未调用飞书或生图服务。它们不对应真实 TOPSTAR 商品，禁止上架。这一页仅用于查看测试证据，日常操作界面在飞书。</p><main>'''+''.join(cards)+'''</main><p>完整记录：demo-summary.json；交付文件位于 runtime/deliveries/。</p></html>'''
        (destination/"index.html").write_text(html,encoding="utf-8")
        return summary
    finally:
        state.close()
