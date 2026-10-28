import ast
import base64
import copy
import hashlib
import io
import json
import tempfile
import textwrap
import time
import unittest
import zlib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from ngs.api import (API_PATH, MAX_BODY, ApiRejected, ApiCallError, MetadataApi, claim_state, validate_headers,
                     validate_payload, request_signature, response_signature)
from ngs.portable import PortableStorage
from tools.metadata_api import protocol_source, metadata_web, claim_device, release_device, deploy, service_session, update_scripts


class ProtocolTests(unittest.TestCase):
    def test_update_check_is_bounded_authenticated_and_does_not_take_transfer_lease(self):
        for action in ('check', 'refresh', 'status'):
            value = dict(operation='updates', value=dict(action=action))
            self.assertEqual(validate_payload(value), ('updates', dict(action=action)))
            namespace = {}; exec(protocol_source(), namespace)
            self.assertEqual(namespace['validate_payload'](value), validate_payload(value))
        for value in ({}, {'action': 'delete'}, {'action': 'check', 'url': 'https://evil.invalid'}, 'check'):
            with self.assertRaises(ValueError): validate_payload(dict(operation='updates', value=value))
        previous = dict(busy_until=9999, busy_nonce='game-transfer')
        state = claim_state(previous, 'update-nonce', 'updates', 1000)
        self.assertEqual(state['busy_nonce'], 'game-transfer')
        with self.assertRaises(ApiRejected): claim_state(state, 'update-nonce', 'updates', 1001)

    def test_legacy_key_alone_cannot_open_api_and_expired_or_other_server_account_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            storage = PortableStorage(root)
            (storage.config/'api-device.json').write_text(json.dumps(dict(schema=1, server='http://nas'+API_PATH, device='a'*32, secret='b'*64)))
            with self.assertRaisesRegex(RuntimeError, 'Войди'): MetadataApi(storage, 'http://nas/tactic')
            account = dict(server='http://nas', profile='c'*32, token='d'*64, expires=2000000000,
                           api=dict(device='a'*32, secret='b'*64, expires=2000000000))
            path = storage.config/'player-account.json'
            path.write_text(json.dumps(dict(account=account)))
            self.assertEqual(MetadataApi(storage, 'http://nas/tactic').device, 'a'*32)
            for change in ({'expires': 1}, {'server': 'http://other'}, {'api': {}}):
                path.write_text(json.dumps(dict(account=dict(account, **change))))
                with self.assertRaises(RuntimeError): MetadataApi(storage, 'http://nas/tactic')

    def test_contract_and_signatures_bind_method_path_body_nonce_status(self):
        secret, device, stamp, nonce = 'ab' * 32, 'cd' * 16, '1792092000', 'ef' * 16
        body = b'{"operation":"search","value":"Game"}'
        signature = request_signature(secret, device, stamp, nonce, body)
        self.assertNotEqual(signature, request_signature(secret, device, stamp, nonce, body + b' '))
        self.assertNotEqual(signature, request_signature(secret, device, stamp, '00' * 16, body))
        self.assertNotEqual(signature, response_signature(secret, nonce, 200, body))
        self.assertNotEqual(response_signature(secret, nonce, 200, body), response_signature(secret, nonce, 400, body))
        ns = {}; exec(protocol_source(), ns)
        self.assertEqual(signature, ns['request_signature'](secret, device, stamp, nonce, body))
        for value in ({'operation':'execute_python_script','value':'code'}, {'operation':'resolve','value':'../1'},
                      {'operation':'search','value':'x','project':'open_heavens_cafe'}, {'operation':'search','value':[]},
                      {'operation':'search','value':'a\n'}):
            with self.assertRaises(ValueError): validate_payload(value)

    def test_replay_expiry_busy_and_bounded_rate_history(self):
        now = 1792092000
        headers = {'X-NGS-Device':'ab'*16,'X-NGS-Time':str(now),'X-NGS-Nonce':'cd'*16,'X-NGS-Signature':'ef'*32}
        self.assertEqual(validate_headers(headers, now)[0], 'ab'*16)
        with self.assertRaises(ApiRejected): validate_headers(headers, now+91)
        state = claim_state({}, 'nonce1', 'search', now)
        for nonce in ('nonce1','nonce2'):
            with self.assertRaises(ApiRejected): claim_state(state, nonce, 'search', now+1)
        state.update(busy_until=0)
        with self.assertRaises(ApiRejected): claim_state(state, 'nonce1', 'search', now+1)
        for i in range(1,60):
            state = claim_state(state, 'n'+str(i), 'search', now+1); state['busy_until']=0
        with self.assertRaises(ApiRejected) as error: claim_state(state, 'another', 'search', now+1)
        self.assertEqual(error.exception.status,429)
        self.assertEqual(len(claim_state(state, 'new', 'search', now+602)['recent']),1)
        with self.assertRaises(ValueError): claim_state({'recent':'corrupt'}, 'n', 'search', now)
        limited = {'recent':[[now,str(i),'resolve'] for i in range(20)]}
        with self.assertRaises(ApiRejected): claim_state(limited,'next','resolve',now)

    def test_deployment_embeds_valid_python_and_keeps_login_form(self):
        calls=[]
        class Client:
            def script(self, code, **kwargs):
                calls.append((code,kwargs))
                ast.parse('def run():\n'+textwrap.indent(code,'    '))
                if 'source' in kwargs: ast.parse('def saved():\n'+textwrap.indent(kwargs['source'],'    '))
                return {}
        deploy(Client())
        self.assertIn('WebLoginWdg2',calls[0][1]['widget'])
        tree=ast.parse('def saved():\n'+textwrap.indent(calls[0][1]['source'],'    '))
        encoded=next(node.args[0].value for node in ast.walk(tree) if isinstance(node,ast.Call)
                     and isinstance(node.func,ast.Attribute) and node.func.attr=='b64decode')
        source=zlib.decompress(base64.b64decode(encoded)).decode('utf8')
        self.assertIn('ngs/metadata',source)
        self.assertNotIn('login_as_batch',source)
        self.assertIn('ngs_metadata_worker',source)

    def test_client_sends_no_tactic_ticket_and_rejects_forged_response(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            storage=PortableStorage(root)
            cfg={'schema':1,'server':'http://nas'+API_PATH,'device':'ab'*16,'secret':'cd'*32}
            (storage.config/'player-account.json').write_text(json.dumps(dict(account=dict(server='http://nas', profile='a'*32, token='b'*64, expires=2000000000, api=dict(device=cfg['device'], secret=cfg['secret'], expires=2000000000)))))
            client=MetadataApi(storage,'http://nas/tactic')
            def open_request(request,timeout):
                self.assertNotIn('Authorization',request.headers)
                self.assertNotIn(cfg['secret'],str(request.headers)+str(request.data))
                nonce=request.get_header('X-ngs-nonce')
                body=b'{"schema":1,"result":[]}'
                response=io.BytesIO(body);response.code=200
                response.headers={'X-NGS-Signature':response_signature(cfg['secret'],nonce,200,body)}
                return response
            with patch.object(client.opener,'open',side_effect=open_request) as opened:
                self.assertEqual(client.call('search','Game'),[])
                self.assertEqual(opened.call_args.kwargs['timeout'], 90)
                self.assertEqual(client.call('transfer', dict(action='transfer', preview='ef'*16, ack=-1)), [])
                self.assertEqual(opened.call_args.kwargs['timeout'], 10)
            forged=io.BytesIO(b'{"schema":1,"result":[]}');forged.code=200;forged.headers={'X-NGS-Signature':'0'*64}
            with patch.object(client.opener,'open',return_value=forged),self.assertRaisesRegex(RuntimeError,'подпись'):
                client.call('search','Game')
            with self.assertRaises(RuntimeError): MetadataApi(storage,'http://other/tactic')

    def test_deployment_refuses_other_project_before_any_schema_or_user_write(self):
        def module(name,**values):
            result=ModuleType(name);result.__dict__.update(values);return result
        touched=[]
        def mutation(*args,**kwargs): touched.append(args);raise AssertionError('Protected project touched')
        modules={'pyasm':module('pyasm'),
            'pyasm.biz':module('pyasm.biz',Project=SimpleNamespace(get=lambda:SimpleNamespace(get_code=lambda:'open_heavens_cafe'))),
            'pyasm.common':module('pyasm.common',Environment=SimpleNamespace(get_security=lambda:SimpleNamespace(is_admin=lambda:True)),Container=None),
            'pyasm.search':module('pyasm.search',Search=mutation,SearchType=None,DbContainer=None),
            'pyasm.security':module('pyasm.security',Login=None)}
        class Client:
            def script(self,code,**kwargs):
                kwargs.pop('timeout',None)
                namespace={}
                exec('def run('+','.join(kwargs)+'):\n'+textwrap.indent(code,'    '),namespace)
                return namespace['run'](**kwargs)
        for operation in (deploy,update_scripts):
            with patch.dict('sys.modules',modules),self.assertRaises(PermissionError): operation(Client())
        self.assertEqual(touched,[])

    def test_only_signed_busy_response_is_classified_for_retry(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            storage=PortableStorage(root)
            cfg=dict(schema=1,server='http://nas'+API_PATH,device='ab'*16,secret='cd'*32)
            (storage.config/'player-account.json').write_text(json.dumps(dict(account=dict(server='http://nas', profile='a'*32, token='b'*64, expires=2000000000, api=dict(device=cfg['device'], secret=cfg['secret'], expires=2000000000)))))
            client=MetadataApi(storage,'http://nas/tactic')
            for reason,status,expected in [('DeviceBusy',409,'busy'),('Request rejected',409,None),('DeviceBusy',403,None)]:
                def response(request,timeout):
                    data=json.dumps(dict(schema=1,error=reason)).encode()
                    result=io.BytesIO(data);result.code=status
                    result.headers={'X-NGS-Signature':response_signature(cfg['secret'],request.get_header('X-ngs-nonce'),status,data)}
                    return result
                with patch.object(client.opener,'open',side_effect=response),self.assertRaises(ApiCallError) as error:
                    client.call('transfer',dict(action='transfer',preview='ef'*16,ack=0))
                self.assertEqual(error.exception.reason,expected)
            forged=io.BytesIO(b'{"schema":1,"error":"DeviceBusy"}');forged.code=409
            forged.headers={'X-NGS-Signature':'0'*64}
            with patch.object(client.opener,'open',return_value=forged),self.assertRaisesRegex(RuntimeError,'подпись'):
                client.call('transfer',dict(action='transfer',preview='ef'*16,ack=0))


class GatewayTests(unittest.TestCase):
    def test_legacy_device_without_account_cannot_reach_backend(self):
        self.legacy_only = True
        self.prepare()
        self.assertEqual(self.call()[0], 401)
        self.assertEqual(self.backend_calls, [])
        self.assertEqual(self.commands, [])

    def test_revoked_profile_session_never_reaches_backend_even_with_valid_api_signature(self):
        self.account_allowed = False
        self.prepare()
        self.assertEqual(self.call()[0], 401)
        self.assertEqual(self.backend_calls, [])
        self.assertEqual(self.state_commits, [])
        self.assertTrue(self.response.headers['X-NGS-Signature'])

    def setUp(self):
        self.secret='ab'*32;self.device='cd'*16;self.backend_calls=[];self.commands=[]
        self.device_values={'code':self.device,'secret':self.secret,'enabled':True,'state':'{}'}
        values=self.device_values
        self.state_commits=[]
        obj=SimpleNamespace(get_value=lambda key:values.get(key),set_value=lambda k,v:values.__setitem__(k,v),
                            commit=lambda **kwargs:self.state_commits.append(kwargs))
        class Search:
            def __init__(self,kind): self.filters={};self.kind=kind
            def add_filter(self,key,value): self.filters[key]=value
            def get_sobject(self):
                if self.kind=='ngs_launcher/catalog_config':
                    return getattr(owner,'catalog_config',None)
                return obj if self.filters.get('code')==values['code'] else None
        self.hooks=[]
        self.request=SimpleNamespace(path_info=API_PATH,query_string='',method='POST',headers={},body=None,
                                     hooks=SimpleNamespace(attach=lambda name,fn,priority:self.hooks.append(fn)))
        self.response=SimpleNamespace(headers={},body=[],status=200)
        owner=self
        class PythonCmd:
            def __init__(self,**kwargs): self.kwargs=kwargs;self.info=None
            def get_info(self): return self.info
        def execute(cmd):
            self.commands.append(cmd.kwargs)
            if cmd.kwargs.get('script_path')=='ngs/catalog':
                self.backend_calls.append(('catalog',cmd.kwargs['action'],cmd.kwargs['device']))
                if getattr(self,'catalog_responses',None):
                    self.assertTrue(json.loads(self.device_values['state'])['busy_nonce'])
                    result=self.catalog_responses.pop(0)
                    if isinstance(result,Exception): raise result
                    cmd.info=result
                else:
                    cmd.info=dict(stage='folders',rows=[],offset=0,total=0)
            elif cmd.kwargs.get('script_path')=='ngs/metadata':
                self.backend_calls.append((cmd.kwargs['operation'],cmd.kwargs['value']))
                if getattr(self,'fail_backend',False): raise OSError('provider unavailable')
                cmd.info=[{'app_id':'12','name':'Game'}]
            else:
                kwargs=dict(cmd.kwargs);source=kwargs.pop('code')
                if 'account' in kwargs: cmd.info=getattr(self,'account_allowed',True)
                elif 'payload' in kwargs:
                    self.backend_calls.append(('diagnostics', kwargs['payload']['action'], kwargs['device']))
                    cmd.info=dict(offset=0, complete=False)
                else: cmd.info=claim_device(**kwargs) if source.startswith('def claim_device') else release_device(**kwargs)
        def mod(name,**kwargs):
            module=ModuleType(name);module.__dict__.update(kwargs);return module
        project=SimpleNamespace(get_code=lambda:'ngs_launcher',get_database_name=lambda:'ngs_launcher',
            get_value=lambda key:'NGS handheld game catalog; managed from D:/code/NGS',get_project_db_resource=lambda:'ngs_launcher')
        self.modules={
            'cherrypy':mod('cherrypy',request=self.request,response=self.response),
            'pyasm':mod('pyasm'),'pyasm.biz':mod('pyasm.biz',Project=SimpleNamespace(get=lambda:project)),
            'pyasm.search':mod('pyasm.search',Search=Search,DbContainer=SimpleNamespace(get=lambda db:SimpleNamespace(do_query=lambda query:None))),
            'pyasm.command':mod('pyasm.command',Command=SimpleNamespace(execute_cmd=execute)),
            'tactic':mod('tactic'),'tactic.command':mod('tactic.command',PythonCmd=PythonCmd)}

    def prepare(self,payload=None,nonce='ef'*16):
        payload=payload or {'operation':'search','value':'Game'}
        body=json.dumps(payload).encode();stamp=str(int(time.time()))
        self.request.headers={'Content-Length':str(len(body)),'Content-Type':'application/octet-stream',
            'X-NGS-Device':self.device,'X-NGS-Time':stamp,'X-NGS-Nonce':nonce,
            'X-NGS-Signature':request_signature(self.secret,self.device,stamp,nonce,body)}
        self.request.body=io.BytesIO(body)

    def call(self):
        import inspect
        self.hooks.clear()
        if not getattr(self, 'legacy_only', False):
            state=json.loads(self.device_values['state']);state.setdefault('account',{'login':'a'*64});self.device_values['state']=json.dumps(state)
        from contextlib import nullcontext
        with patch.dict('sys.modules',self.modules), patch('tools.metadata_api.service_session',return_value=nullcontext()):
            metadata_web(protocol_source(),inspect.getsource(claim_device),inspect.getsource(release_device),account_source='account-check')
        self.assertEqual(len(self.hooks),1)
        self.hooks[0]()
        return self.response.status,json.loads(b''.join(self.response.body))

    def test_success_replay_and_failure_release_lease(self):
        self.prepare();self.assertEqual(self.call()[0],200)
        self.assertEqual(self.backend_calls,[('search','Game')])
        self.assertEqual(json.loads(self.device_values['state'])['busy_until'],0)
        self.assertEqual(self.state_commits,[dict(triggers='none',log_transaction=False)]*2)
        self.prepare();self.assertEqual(self.call()[0],409)
        self.assertEqual(len(self.backend_calls),1)
        self.fail_backend=True;self.prepare(nonce='00'*16)
        self.assertEqual(self.call()[0],503)
        self.assertEqual(json.loads(self.device_values['state'])['busy_until'],0)

    def test_busy_is_distinct_from_replay_and_preserves_other_request_lease(self):
        active=claim_state({'account':{'login':'a'*64}},'ef'*16,'search',int(time.time()))
        self.device_values['state']=json.dumps(active)
        self.prepare(nonce='00'*16)
        self.assertEqual(self.call(),(409,dict(schema=1,error='DeviceBusy')))
        self.assertEqual(json.loads(self.device_values['state']),active)
        self.assertEqual(self.backend_calls,[])
        self.assertEqual(self.state_commits,[])
        self.assertEqual(self.response.headers['X-NGS-Signature'],
                         response_signature(self.secret,'00'*16,409,b''.join(self.response.body)))
        self.prepare(nonce='ef'*16)
        self.assertEqual(self.call(),(409,dict(schema=1,error='Request rejected')))

    def test_catalogizer_allows_logged_in_account_without_legacy_device_list(self):
        self.prepare(dict(operation='catalog',value=dict(action='list',offset=0)))
        self.assertEqual(self.call()[0],403)
        self.assertEqual(self.backend_calls,[])
        self.catalog_config=SimpleNamespace(get_value=lambda key:'NGS remote catalogizer v1' if key=='description'
            else json.dumps(dict(devices=[])))
        self.prepare(dict(operation='catalog',value=dict(action='list',offset=0)))
        status,result=self.call()
        self.assertEqual(status,200)
        self.assertEqual(result['result']['stage'],'folders')
        self.assertEqual(self.backend_calls,[('catalog','list',self.device)])
        self.assertEqual(json.loads(self.device_values['state'])['busy_until'],0)

    def test_invalid_requests_and_revoked_keys_never_reach_backend(self):
        self.prepare({'operation':'search','value':'Game','code':'return secret'})
        self.assertEqual(self.call()[0],400)
        self.prepare();self.request.headers['X-NGS-Signature']='0'*64
        self.assertEqual(self.call()[0],401)
        self.prepare();self.request.headers['Content-Length']=str(MAX_BODY+1)
        self.assertEqual(self.call()[0],413)
        self.prepare();self.request.method='GET';self.assertEqual(self.call()[0],405)
        self.request.method='POST';self.prepare();self.request.query_string='script_path=anything'
        self.assertEqual(self.call()[0],404)
        self.request.query_string='';self.prepare();self.device_values['enabled']=False
        self.assertEqual(self.call()[0],401)
        self.assertEqual(self.backend_calls,[])
        self.assertTrue(all('account' in c for c in self.commands))

    def test_diagnostics_signed_response_and_release_preserve_active_transfer_lease(self):
        payload = dict(operation='diagnostics', value=dict(action='status', session='01'*16, sha256='02'*32, size=100))
        self.prepare(payload)
        self.assertEqual(self.call()[0], 403)
        self.catalog_config=SimpleNamespace(get_value=lambda key:'NGS remote catalogizer v1' if key=='description'
            else json.dumps(dict(devices=[])))
        active = claim_state({}, 'transfer', 'transfer', int(time.time()))
        self.device_values['state'] = json.dumps(active)
        self.prepare(payload)
        status, result = self.call()
        self.assertEqual(status, 200)
        self.assertEqual(result['result'], dict(offset=0, complete=False))
        self.assertEqual(self.backend_calls, [('diagnostics','status',self.device)])
        state = json.loads(self.device_values['state'])
        self.assertEqual(state['busy_nonce'], active['busy_nonce'])
        self.assertEqual(state['busy_until'], active['busy_until'])
        self.assertEqual(state['recent'], active['recent'])
        raw = b''.join(self.response.body)
        self.assertEqual(self.response.headers['X-NGS-Signature'], response_signature(self.secret, 'ef'*16, 200, raw))
        self.prepare(payload)
        self.assertEqual(self.call()[0], 409)

    def test_publish_completes_committed_export_and_activation_without_more_http_roundtrips(self):
        self.catalog_config=SimpleNamespace(get_value=lambda key:'NGS remote catalogizer v1' if key=='description'
            else json.dumps(dict(devices=[])))
        cases=[(prefix,action,stage) for prefix in ('','installer_') for action,stage in
               (('publish','working'),('status','working'),('status','batch'))]
        for i,(prefix,action,stage) in enumerate(cases):
            nonce=f'{i:032x}'
            self.backend_calls.clear()
            self.catalog_responses=[dict(stage=stage,state='export'),dict(stage='working',state='activate'),dict(stage='done',state='done')]
            self.prepare(dict(operation='catalog',value=dict(action=prefix+action,preview='ab'*16)),nonce=nonce)
            status,result=self.call()
            self.assertEqual(status,200)
            self.assertEqual(result['result']['state'],'done')
            self.assertEqual([call[1] for call in self.backend_calls],[prefix+action,prefix+'status',prefix+'status'])
            self.assertEqual(json.loads(self.device_values['state'])['busy_nonce'],'')
            self.prepare(dict(operation='catalog',value=dict(action=prefix+action,preview='ab'*16)),nonce=nonce)
            self.assertEqual(self.call()[0],409)
            self.assertEqual(len(self.backend_calls),3)

    def test_provider_error_message_is_signed_and_releases_the_lease(self):
        self.catalog_config=SimpleNamespace(get_value=lambda key:'NGS remote catalogizer v1' if key=='description'
            else json.dumps(dict(devices=[])))
        message='Источники оформления временно недоступны. Повтори поиск или добавь игру без оформления.'
        self.catalog_responses=[ValueError(message)]
        self.prepare(dict(operation='catalog',value=dict(action='installer_search',query='Unknown Game')))
        status,result=self.call()
        self.assertEqual(status,400)
        self.assertEqual(result['error'],message)
        self.assertEqual(json.loads(self.device_values['state'])['busy_nonce'],'')
        self.assertEqual(self.response.headers['X-NGS-Signature'],
                         response_signature(self.secret,'ef'*16,400,b''.join(self.response.body)))

    def test_failed_export_keeps_resume_possible_and_continuation_is_bounded(self):
        self.catalog_config=SimpleNamespace(get_value=lambda key:'NGS remote catalogizer v1' if key=='description'
            else json.dumps(dict(devices=[])))
        self.catalog_responses=[dict(stage='working',state='export'),OSError('export failed')]
        self.prepare(dict(operation='catalog',value=dict(action='publish',preview='ab'*16)))
        self.assertEqual(self.call()[0],503)
        self.assertEqual(json.loads(self.device_values['state'])['busy_nonce'],'')
        self.assertEqual([call[1] for call in self.backend_calls],['publish','status'])
        self.backend_calls.clear()
        self.catalog_responses=[dict(stage='working',state='export')]*4
        self.prepare(dict(operation='catalog',value=dict(action='publish',preview='ab'*16)),nonce='11'*16)
        status,result=self.call()
        self.assertEqual(status,200)
        self.assertEqual(result['result']['state'],'export')
        self.assertEqual(len(self.backend_calls),3)

    def test_stream_download_requires_account_and_catalog_service(self):
        self.prepare(dict(operation='transfer',value=dict(action='estimate',game='game',revision=1)))
        self.assertEqual(self.call()[0],403)
        self.assertEqual(self.backend_calls,[])
        self.catalog_config=SimpleNamespace(get_value=lambda key:'NGS remote catalogizer v1' if key=='description'
            else json.dumps(dict(devices=[])))
        self.prepare(dict(operation='transfer',value=dict(action='estimate',game='game',revision=1)))
        self.assertEqual(self.call()[0],200)
        self.assertEqual(self.backend_calls,[('catalog','estimate',self.device)])


class ServiceSessionTests(unittest.TestCase):
    def test_real_non_admin_login_stays_server_side_and_context_is_restored(self):
        def module(name,**values):
            result=ModuleType(name);result.__dict__.update(values);return result
        previous=object();current=[previous];logins=[];project=['ngs_launcher']
        server=SimpleNamespace(ticket={'ticket':''},login_ticket='',login=None,handoff_dir=None,
                               get_protocol=lambda:'local')
        original=copy.deepcopy(server.__dict__)
        def set_ticket(value): server.ticket={'ticket':value};server.login_ticket=value
        server.set_ticket=set_ticket
        credential=SimpleNamespace(get_value=lambda key:{'login':'ngs_metadata_worker','password':'server-password'}[key])
        class Search:
            def __init__(self,kind): self.kind=kind
            def add_filter(self,*args): pass
            def add_order_by(self,*args): pass
            def set_limit(self,*args): pass
            def get_sobject(self): return credential if self.kind=='ngs_launcher/api_service' else None
        class Security:
            def login_user(self,login,password):
                logins.append((login,password));project[0]='admin'
            def is_logged_in(self): return True
            def is_admin(self): return False
            def get_ticket_key(self): return 'server-only-session-ticket'
        modules={'pyasm':module('pyasm'),'pyasm.common':module('pyasm.common',Environment=SimpleNamespace(
            get_security=lambda:current[0],set_security=lambda value:current.__setitem__(0,value))),
            'pyasm.search':module('pyasm.search',Search=Search),
            'pyasm.biz':module('pyasm.biz',Project=SimpleNamespace(
                get_project_code=lambda:project[0],set_project=lambda value:project.__setitem__(0,value))),
            'pyasm.security':module('pyasm.security',Security=Security,Ticket=None),
            'tactic_client_lib':module('tactic_client_lib',TacticServerStub=SimpleNamespace(get=lambda **kwargs:server))}
        with patch.dict('sys.modules',modules):
            for fail in (False,True):
                try:
                    with service_session():
                        self.assertIsInstance(current[0],Security)
                        self.assertEqual(project[0],'ngs_launcher')
                        self.assertEqual(server.login_ticket,'server-only-session-ticket')
                        if fail: raise RuntimeError('provider failed')
                except RuntimeError: pass
                self.assertIs(current[0],previous)
                self.assertEqual(project[0],'ngs_launcher')
                self.assertEqual(server.ticket,original['ticket'])
                self.assertEqual(server.login_ticket,original['login_ticket'])
        self.assertEqual(logins,[('ngs_metadata_worker','server-password')]*2)


if __name__=='__main__': unittest.main()
