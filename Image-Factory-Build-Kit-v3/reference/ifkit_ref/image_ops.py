"""Optional Pillow helpers. Uses actual alpha, never white-color threshold removal.
The helpers check geometry/format, NOT brand accuracy or visual quality.
"""
from pathlib import Path
from .contracts import Blocked

def _image(path):
    from PIL import Image
    with Image.open(path) as im:
        if im.width*im.height > 32_000_000:
            raise Blocked('Image too large for reference compositor')
        im.load()
        return im.copy()

def validate_product(path):
    im=_image(path)
    if im.mode!='RGBA':
        raise Blocked('A real RGBA product source is required; white background is not transparent')
    alpha=im.getchannel('A')
    low,high=alpha.getextrema()
    hist=alpha.histogram()
    total=im.width*im.height
    transparent=hist[0]/total
    visible=sum(hist[1:])/total
    bbox=alpha.getbbox()
    if low!=0 or high!=255 or transparent<0.02 or visible<0.02 or bbox is None:
        raise Blocked('Missing meaningful transparency or subject pixels')
    if min(bbox[2]-bbox[0],bbox[3]-bbox[1])<4:
        raise Blocked('Degenerate product alpha bounds')
    return im,{'width':im.width,'height':im.height,'bbox':list(bbox),'transparent_fraction':transparent}

def white_preview(source,target):
    from PIL import Image
    im,meta=validate_product(source)
    white=Image.new('RGBA',im.size,(255,255,255,255))
    white.alpha_composite(im)
    white.convert('RGB').save(target,format='PNG')
    return meta

def composite(source,background,target):
    from PIL import Image
    fg,meta=validate_product(source)
    bg=_image(background).convert('RGBA')
    if bg.width!=bg.height:
        raise Blocked('Background is not square; choose and log an explicit crop policy first')
    fg=fg.crop(tuple(meta['bbox']))
    scale=min(bg.width*0.62/fg.width,bg.height*0.38/fg.height)
    size=(max(1,round(fg.width*scale)),max(1,round(fg.height*scale)))
    fg=fg.resize(size,Image.Resampling.LANCZOS)
    xy=((bg.width-size[0])//2,round(bg.height*0.83)-size[1])
    bg.alpha_composite(fg,xy)
    bg.convert('RGB').save(target,format='PNG')
    return {'source':meta,'output_size':list(bg.size),'product_size':list(size),
            'position':list(xy),'algorithm':'alpha-composite-v1','shadow':'none',
            'warning':'Perspective, lighting and contact require visual inspection'}
