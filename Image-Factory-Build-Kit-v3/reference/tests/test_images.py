import tempfile
import unittest
from pathlib import Path
from ifkit_ref.contracts import Blocked
try:
    from PIL import Image, ImageDraw
    PILLOW=True
except ImportError:
    PILLOW=False
from ifkit_ref.image_ops import validate_product,white_preview,composite

@unittest.skipUnless(PILLOW,'Optional Pillow missing; core suite requires only standard library')
class ImageTests(unittest.TestCase):
    def setUp(self):
        self.d=tempfile.TemporaryDirectory(); self.root=Path(self.d.name)
        self.src=self.root/'source.png'; self.bg=self.root/'bg.png'; self.out=self.root/'out.png'
        im=Image.new('RGBA',(100,80),(0,0,0,0)); ImageDraw.Draw(im).rectangle((20,20,80,60),fill=(255,255,255,255)); im.save(self.src)
        Image.new('RGB',(256,256),(100,150,220)).save(self.bg)
    def tearDown(self): self.d.cleanup()
    def test_transparent_source_passes(self): self.assertGreater(validate_product(self.src)[1]['transparent_fraction'],0)
    def test_white_background_rejected_as_source(self):
        Image.new('RGB',(100,100),'white').save(self.src)
        with self.assertRaises(Blocked): validate_product(self.src)
    def test_fake_rgba_opaque_rejected(self):
        Image.new('RGBA',(100,100),(255,255,255,255)).save(self.src)
        with self.assertRaises(Blocked): validate_product(self.src)
    def test_fully_transparent_rejected(self):
        Image.new('RGBA',(100,100),(0,0,0,0)).save(self.src)
        with self.assertRaises(Blocked): validate_product(self.src)
    def test_white_source_not_erased(self):
        composite(self.src,self.bg,self.out)
        with Image.open(self.out) as im: self.assertEqual(im.getpixel((128,175)),(255,255,255))
    def test_white_preview_separate_file(self):
        white_preview(self.src,self.out)
        with Image.open(self.out) as im: self.assertEqual(im.mode,'RGB')
        with Image.open(self.src) as im: self.assertEqual(im.mode,'RGBA')
    def test_nonsquare_not_silently_cropped(self):
        Image.new('RGB',(100,200)).save(self.bg)
        with self.assertRaises(Blocked): composite(self.src,self.bg,self.out)
