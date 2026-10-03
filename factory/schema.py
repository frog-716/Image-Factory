"""Stable OpenAPI field types. Display names are a contract; drift is rejected."""
from __future__ import annotations

STATES = ["草稿", "待生图", "生成中", "待审核", "已完成", "已取消", "需人工核对", "阻塞"]

def field(name, kind=1, **kwargs):
    return {"field_name": name, "type": kind, **kwargs}

def select(name, options):
    return field(name, 3, property={"options": [{"name": x} for x in options]})

def link(name, table, multiple=True):
    return {"field_name": name, "type": 18, "target": table, "multiple": multiple}

TABLES = {
    "products": {"name": "01 商品", "fields": [
        field("商品名"), field("SKU"), field("商品品牌"), field("事实版本"), field("已确认事实"),
        field("禁止改变"), field("事实来源"), field("资料已确认",7), field("示例数据",7),
        field("商品照片",17), link("默认商品素材","assets"),
    ]},
    "assets": {"name": "02 素材与成图", "fields": [
        field("素材名"), field("资产ID"), select("类型",["商品原图","风格参考","场景参考","Logo","生成成图"]),
        field("图片",17), link("商品","products"), field("来源说明"), field("授权说明"),
        field("允许用于生图",7), field("SHA256"), field("示例数据",7), link("来源任务","tasks",False),
        field("槽位"), field("运行ID"), select("审核状态",["待审核","通过","驳回","撤回"]), field("生产回执",17),
        select("模式",["demo","production"]),
        select("资产角色",["product_rgba","product_white","background","candidate"]),
    ]},
    "workflows": {"name": "03 流程", "fields": [
        field("流程名"), field("版本"), select("生产方式",["背景合成","参考图编辑"]),
        field("已发布",7), field("说明"), field("输出宽度",2), field("输出高度",2),
        field("商品占画布比例",2), field("示例数据",7),
    ]},
    "steps": {"name": "04 流程步骤", "fields": [
        field("步骤名"), link("流程","workflows",False), field("顺序",2),
        select("节点类型",["提示词","生图","原图合成","文件检查","人工审核"]),
        field("提示词模板"), link("参考素材","assets"), field("启用",7),
        field("组件ID"), field("组件版本"),
    ]},
    "tasks": {"name": "05 生产任务", "fields": [
        field("任务名"), select("选择商品",[]),
        select("创建确认",["确认创建演示任务"]),
        link("商品","products",False), link("流程","workflows",False),
        link("选用素材","assets"), field("渠道"), field("图片用途"), field("消费场景"), field("版位"),
        field("视觉风格"), field("附加要求"), field("变体方向"), field("数量",2), field("最多调用次数",2),
        field("提交",7), field("取消",7), field("示例数据",7), select("系统状态",STATES),
        field("运行ID"), field("错误摘要"), field("最新档案SHA256"), field("运行档案",17), field("已批准交付包",17),
        link("返工来源","tasks",False), field("命名空间"), select("模式",["demo","production"]),
        link("选用提示词组件","steps"), field("审核策略版本"), field("运行版本",2),
    ]},
    "reviews": {"name": "06 人工审核", "fields": [
        field("审核标题"), link("图片资产","assets",False), field("目标SHA256"),
        select("结论",["通过","驳回","撤回"]), field("审核人",11,property={"multiple":False}),
        field("商品准确",7), field("品牌及渠道检查",7), field("人工确认",7), field("原因"),
        field("Demo视觉检查",7), field("仅限演示使用",7), field("审核策略版本"),
        field("运行ID"), field("审核版本",2), field("创建时间",1001), field("创建人",1003),
    ]},
    "placements": {"name": "07 上架记录", "fields": [
        field("使用记录名"), field("使用ID"), link("图片资产","assets",False), field("目标SHA256"),
        field("渠道"), field("店铺或账户"), field("版位"), field("广告或页面ID"), field("上线时间"),
        field("下线时间"), field("实验组"), field("对照说明"), field("人工确认已上线",7), field("证据",17),
        select("模式",["demo","production"]),
    ]},
    "metrics": {"name": "08 效果数据", "fields": [
        field("指标记录名"), field("指标ID"), field("逻辑键"), field("版本",2), field("取代指标ID"), link("使用记录","placements",False),
        field("日期"), field("时区"), field("渠道"), field("曝光",2), field("点击",2), field("订单",2),
        field("点击率",2), field("每点击订单率",2), field("指标口径"), field("数据性质"),
        field("来源文件SHA256"), field("来源文件",17), field("样本提醒"), select("模式",["demo","production"]),
    ]},
}

TASK_INPUTS = ["任务名","商品","流程","选用素材","渠道","图片用途","消费场景","版位","视觉风格","附加要求","变体方向","数量","最多调用次数","示例数据","返工来源","命名空间","模式","选用提示词组件","审核策略版本","运行版本"]
# Human form inputs are a separate boundary; they are not frozen task inputs.
FORM_INPUTS = ["选择商品", "图片用途", "消费场景", "视觉风格", "数量", "附加要求", "创建确认"]
PRODUCT_INPUTS = ["商品名","SKU","商品品牌","事实版本","已确认事实","禁止改变","事实来源","资料已确认","示例数据","默认商品素材"]
WORKFLOW_INPUTS = ["流程名","版本","生产方式","已发布","输出宽度","输出高度","商品占画布比例","示例数据"]
STEP_INPUTS = ["步骤名","流程","顺序","节点类型","提示词模板","参考素材","启用","组件ID","组件版本"]
ASSET_INPUTS = ["资产ID","类型","商品","来源说明","授权说明","允许用于生图","SHA256","示例数据","模式","资产角色"]
