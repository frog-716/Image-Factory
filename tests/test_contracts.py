import json,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from factory.util import FactoryError,UnknownWrite,confined,integer,read_json,safe_id,links,text
from factory.lark import FeishuBase,LarkCLI
from factory.metrics import parse_metrics,COLUMNS
from factory.state import State
from factory.media import inspect_image
from PIL import Image

class UtilityTests(unittest.TestCase):
    def test_traversal_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            for p in ('../secret','/tmp/secret','a\\b',''):
                with self.subTest(p=p),self.assertRaises(FactoryError):confined(Path(d),p,False)
    def test_symlink_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);(r/'real').write_text('x');(r/'link').symlink_to(r/'real')
            with self.assertRaises(FactoryError):confined(r,'link')
    def test_safe_identifier(self):
        self.assertEqual(safe_id('rec_123-a'),'rec_123-a')
        with self.assertRaises(FactoryError):safe_id('rec;touch /tmp/x')
    def test_integer_rejects_bool_fraction_nan_infinity(self):
        for x in (True,1.5,float('nan'),float('inf'),-1,'1'):
            with self.subTest(x=x),self.assertRaises(FactoryError):integer(x,'count')
    def test_integer_allows_integral_feishu_number(self):self.assertEqual(integer(3.0,'count'),3)
    def test_json_rejects_nan(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bad.json';p.write_text('{"x":NaN}')
            with self.assertRaises(FactoryError):read_json(p)
    def test_links_never_guesses_titles(self):
        self.assertEqual(links([{'record_id':'rec_one'}]),['rec_one'])
        self.assertEqual(links([{'id':'rec_two'}]),['rec_two'])
        with self.assertRaises(FactoryError):links([{'text':'商品一'}])
    def test_text_accepts_typed_select_values(self):
        self.assertEqual(text(['背景合成']),'背景合成')
    def test_corrupt_image_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x.png';p.write_bytes(b'not an image')
            with self.assertRaises(FactoryError):inspect_image(p)
    def test_small_and_oversized_image_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x.png';Image.new('RGB',(16,16)).save(p)
            with self.assertRaises(FactoryError):inspect_image(p)
            Image.new('RGB',(64,64)).save(p)
            with self.assertRaises(FactoryError):inspect_image(p,max_bytes=1)

class TransportTests(unittest.TestCase):
    def test_modern_envelope(self):self.assertEqual(LarkCLI.decode('{"ok":true,"data":{"x":1}}','',0),{'x':1})
    def test_legacy_envelope(self):self.assertEqual(LarkCLI.decode('{"code":0,"data":{"x":1}}','',0),{'x':1})

    def test_verified_user_identity_requires_ready_valid_open_id(self):
        status={"identity":"user","identities":{"user":{"available":True,"status":"ready",
                "tokenStatus":"valid","openId":"ou_trusted","userName":"Reviewer"}}}
        self.assertEqual(LarkCLI.user_from_auth_status(status),
                         {"open_id":"ou_trusted","name":"Reviewer","verified":True})
        for changed in (
            {**status,"identity":"bot"},
            {"identity":"user","identities":{"user":{"available":False,"status":"ready",
             "tokenStatus":"valid","openId":"ou_trusted"}}},
            {"identity":"user","identities":{"user":{"available":True,"status":"ready",
             "tokenStatus":"expired","openId":"ou_trusted"}}},
        ):
            with self.subTest(changed=changed),self.assertRaises(FactoryError):
                LarkCLI.user_from_auth_status(changed)
    def test_nested_error_not_ignored(self):
        with self.assertRaises(FactoryError):LarkCLI.decode('{"ok":true,"data":{"code":999,"data":{}}}','',0)
    def test_stderr_error(self):
        with self.assertRaises(FactoryError):LarkCLI.decode('','{"ok":false,"error":{"code":"403"}}',1)
    def test_bad_json_or_array(self):
        for raw in ('plain text','[]'):
            with self.subTest(raw=raw),self.assertRaises(FactoryError):LarkCLI.decode(raw,'',0)
    def test_no_shell_interpolation(self):
        cli=object.__new__(LarkCLI);cli.bin='/test/lark-cli';cli.identity='user';cli.timeout=3
        with patch('factory.lark.subprocess.run',return_value=Mock(returncode=0,stdout='{"ok":true,"data":{}}',stderr='')) as run:
            cli.call(['base','+record-list','--table-id','tbl; touch /tmp/no'])
            self.assertFalse(run.call_args.kwargs['shell']);self.assertIn('tbl; touch /tmp/no',run.call_args.args[0])
    def test_timeout_write_is_unknown(self):
        cli=object.__new__(LarkCLI);cli.bin='x';cli.identity='user';cli.timeout=1
        with patch('factory.lark.subprocess.run',side_effect=subprocess.TimeoutExpired('x',1)):
            with self.assertRaises(UnknownWrite):cli.call(['x'],mutation=True)

    def test_attachment_timeout_terminates_process_group(self):
        cli=object.__new__(LarkCLI);cli.bin='x';cli.identity='user';cli.timeout=1;cli.attachment_timeout=7
        process=Mock(pid=4321,returncode=None)
        process.communicate.side_effect=[subprocess.TimeoutExpired('x',7),('','')]
        with patch('factory.lark.subprocess.Popen',return_value=process),patch('factory.lark.os.killpg') as killpg:
            with self.assertRaises(UnknownWrite):cli.call_attachment(['base','+record-upload-attachment'],mutation=True)
        self.assertEqual(killpg.call_count,1)
        self.assertEqual(cli.last_diagnostic['timeout_seconds'],7)
        self.assertTrue(cli.last_diagnostic['process_group_terminated'])

    def test_structured_validation_error_is_not_unknown_write(self):
        cli=object.__new__(LarkCLI);cli.bin='x';cli.identity='user';cli.timeout=3
        stderr='{"ok":false,"error":{"type":"validation","message":"file not accessible"}}'
        with patch('factory.lark.subprocess.run',return_value=Mock(returncode=2,stdout='',stderr=stderr)):
            with self.assertRaises(FactoryError) as caught:
                cli.call(['base','+record-upload-attachment'],mutation=True)
        self.assertNotIsInstance(caught.exception,UnknownWrite)

class EffectTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.s=State(Path(self.temp.name))
    def tearDown(self):self.s.close();self.temp.cleanup()
    def test_base_binding_cannot_switch(self):
        self.s.bind_base('base_a');self.s.bind_base('base_a')
        with self.assertRaises(FactoryError):self.s.bind_base('base_b')
    def test_unknown_write_never_blind_retries(self):
        write=Mock(side_effect=RuntimeError('disconnected'))
        with self.assertRaises(RuntimeError):self.s.effect('k',lambda:None,write)
        with self.assertRaises(UnknownWrite):self.s.effect('k',lambda:None,write)
        self.assertEqual(write.call_count,1)
    def test_reconciles_remote_success_after_timeout(self):
        with self.assertRaises(RuntimeError):self.s.effect('k',lambda:None,Mock(side_effect=RuntimeError()))
        write=Mock();self.assertEqual(self.s.effect('k',lambda:{'id':'r'},write),{'id':'r'});write.assert_not_called()
    def test_done_is_idempotent(self):
        write=Mock(return_value={'id':'r'})
        for _ in range(2):self.s.effect('k',lambda:None,write)
        self.assertEqual(write.call_count,1)
    def test_retry_requires_human_ack(self):
        with self.assertRaises(FactoryError):self.s.resolve_effect('k',False)

    def test_confirmed_absent_allows_new_attachment_operation(self):
        first=Mock(side_effect=UnknownWrite('timeout'))
        with self.assertRaises(UnknownWrite):self.s.effect('attachment-old',lambda:None,first)
        self.s.resolve_effect('attachment-old',True)
        second=Mock(return_value={'file_token':'new'})
        self.assertEqual(self.s.effect('attachment-new',lambda:None,second),{'file_token':'new'})
        self.assertEqual(second.call_count,1)

    def test_confirmed_success_prevents_duplicate_attachment_upload(self):
        first=Mock(side_effect=UnknownWrite('timeout'))
        with self.assertRaises(UnknownWrite):self.s.effect('attachment',lambda:None,first)
        found={'file_token':'confirmed'}
        duplicate=Mock()
        self.assertEqual(self.s.effect('attachment',lambda:found,duplicate),found)
        self.assertEqual(self.s.effect('attachment',lambda:found,duplicate),found)
        duplicate.assert_not_called()


class AttachmentGatewayTests(unittest.TestCase):
    class Transport:
        def __init__(self, upload_error=None, visible_after=None):
            self.upload_error=upload_error
            self.visible_after=visible_after
            self.record_reads=0
            self.upload_calls=0
        def call(self,args,mutation=False):
            command=args[1]
            if command=='+field-list':
                return {'fields':[{'id':'fld_image','name':'图片','type':'attachment'}]}
            if command=='+record-list':
                self.record_reads+=1
                visible=self.visible_after is not None and self.record_reads>=self.visible_after
                return {'fields':['图片'],'data':[[[{'file_token':'tok','name':'large.png','size':2000000}]] if visible else [[]]],'record_id_list':['rec_probe'],'has_more':False}
            if command=='+record-upload-attachment':
                self.upload_calls+=1
                if self.upload_error:raise self.upload_error
                return {'attachments':{'rec_probe':{'fld_image':[{'file_token':'tok'}]}}}
            raise AssertionError(args)

    def gateway(self,transport):
        return FeishuBase({'base_token':'BASE_TEST','tables':{'assets':'tbl_assets'},'attachment_readback_delays_seconds':[0,0]},transport)

    def test_upload_success_but_bind_not_visible_is_unknown(self):
        transport=self.Transport()
        base=self.gateway(transport)
        with tempfile.TemporaryDirectory() as d,patch('factory.lark.time.sleep'):
            source=Path(d)/'large.png';source.write_bytes(b'x'*2000000)
            with self.assertRaises(UnknownWrite):base.upload_attachment('assets','rec_probe','图片',source)
        self.assertEqual(transport.upload_calls,1)

    def test_timeout_reconciles_visible_binding_without_duplicate_upload(self):
        transport=self.Transport(UnknownWrite('timeout'),visible_after=2)
        base=self.gateway(transport)
        with tempfile.TemporaryDirectory() as d,patch('factory.lark.time.sleep'):
            source=Path(d)/'large.png';source.write_bytes(b'x'*2000000)
            result=base.upload_attachment('assets','rec_probe','图片',source)
        self.assertEqual(result['file_token'],'tok')
        self.assertEqual(transport.upload_calls,1)

class MetricParserTests(unittest.TestCase):
    def csv(self,row=None):return ','.join(COLUMNS)+'\n'+','.join(row or ['P1','2026-09-02','Asia/Shanghai','TEST','0','0','0','orders_per_click','demo'])+'\n'
    def test_zero_denominator_is_unknown(self):
        r=parse_metrics(self.csv())[0];self.assertIsNone(r['ctr']);self.assertIsNone(r['orders_per_click'])
    def test_raw_counts_and_formula_validation(self):
        for v in ('-1','NaN','1.5','=1+1','1%'):
            row=['P1','2026-09-02','Asia/Shanghai','TEST',v,'0','0','orders_per_click','demo']
            with self.subTest(v=v),self.assertRaises(FactoryError):parse_metrics(self.csv(row))
    def test_clicks_cannot_exceed_impressions(self):
        with self.assertRaises(FactoryError):parse_metrics(self.csv(['P1','2026-09-02','UTC','TEST','1','2','0','test','demo']))
    def test_duplicate_logical_rows_rejected(self):
        data=self.csv();data+=data.splitlines()[1]+'\n'
        with self.assertRaises(FactoryError):parse_metrics(data)
    def test_duplicate_header_rejected(self):
        data=self.csv().replace('placement_id,','placement_id,placement_id,',1)
        with self.assertRaises(FactoryError):parse_metrics(data)
    def test_invalid_timezone(self):
        with self.assertRaises(FactoryError):parse_metrics(self.csv().replace('Asia/Shanghai','Not_A_Zone'))
    def test_manual_verifiable_input_supported(self):self.assertEqual(parse_metrics(self.csv().replace('demo','verified_manual'))[0]['source_kind'],'verified_manual')
