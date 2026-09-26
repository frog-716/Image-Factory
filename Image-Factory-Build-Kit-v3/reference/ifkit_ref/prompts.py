"""Fixed allowlisted visual recipes. Governance stays in the frozen request, not pixel text."""
from .contracts import Blocked, ContractError, digest
SCENES = {
 'outdoor': '空旷的现代儿童活动区，浅蓝远景和少量柔和绿植；前景为平整浅灰运动地面。',
 'indoor': '奶油白儿童房，浅木色地面；后方仅少量无品牌积木，背景虚化。',
 'studio': '奶油白无缝摄影棚，淡蓝渐变远景；平整浅色承托面，无装饰物。',
}
PRODUCT_SOURCE = '''独立生成一张无品牌虚构童鞋商品原图：一双完整的白色和浅蓝色儿童运动鞋，
三分之四侧前视角，真实商业产品摄影质感。鞋带、鞋底和鞋口清晰，主体完整不截断。
柔和左上方主光，主体之间保留空隙，四周留边。透明背景 PNG，不画白底，不画棋盘格。
不添加人物、文字、Logo、尺码、价格、认证、功能图标或营销宣称。只输出这一张商品图。'''

def background(scene: str, refs: list[dict] | None = None) -> dict:
    if scene not in SCENES:
        raise ContractError('Unknown scenario')
    refs = refs or []
    attachments=[]
    for r in refs:
        if r.get('use')=='image_reference':
            if r.get('allow_image_generation') is not True:
                raise Blocked('Image reference lacks explicit generation permission')
            attachments.append({'asset_id':r['asset_id'],'sha256':r['sha256']})
        elif r.get('use')=='description_reference':
            if r.get('description_source')!='human_authored' or r.get('allow_description_reuse') is not True:
                raise Blocked('Do not bypass restrictions by auto-captioning forbidden images')
        else:
            raise ContractError('Reference role must be explicit')
    text = ('独立生成一张正方形童鞋广告背景，只画背景。'+SCENES[scene]+
        '机位低，柔和左上方主光，偏右后方轻柔投影。中心偏下约六成宽度留作商品摆放区，'
        '该区域无障碍物。明亮干净、低饱和、真实商业摄影。禁止鞋子、人物、文字、Logo、'
        '尺寸、认证、价格、促销符号和拼图。不得在地面预画鞋形阴影。')
    return {'visual_prompt':text,'prompt_hash':digest(text),'image_references':attachments,
            'output_role':'background','aspect_ratio':'1:1'}
