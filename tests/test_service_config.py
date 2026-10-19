"""Exercise external-service guards without importing board or web-server startup code."""
import ast
import datetime
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
SERVER=ROOT/'src/server.py'
if not SERVER.exists():
    SERVER=ROOT/'supporting-python/sever.py'

def load_service_functions():
    names={'audio_to_text','chat_with_hunyuan','analyze_pill_with_ai','parse_schedule_with_hunyuan'}
    tree=ast.parse(SERVER.read_text(encoding='utf-8'))
    nodes=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in names]
    assert len(nodes)==4
    namespace={'TENCENT_API_KEY':'test-key','TENCENT_CHAT_URL':'',
               'SILICONFLOW_API_KEY':'test-key','SILICONFLOW_TRANSCRIPTION_URL':''}
    class NoNetwork:
        def __init__(self):
            self.calls=[]
        def post(self,*args,**kwargs):
            self.calls.append((args,kwargs))
            raise AssertionError('Unconfigured services must not perform a request')
    namespace['requests']=NoNetwork()
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(SERVER),'exec'),namespace)
    return namespace

class ServiceConfigurationTests(unittest.TestCase):
    def test_missing_urls_do_not_make_network_requests(self):
        service=load_service_functions()
        self.assertEqual(service['audio_to_text']('not-a-real-file.wav'),'')
        self.assertEqual(service['chat_with_hunyuan']('test'),'云端对话服务未配置。')
        self.assertEqual(service['analyze_pill_with_ai']('not-a-real-image.jpg'),(None,'Missing TENCENT_CHAT_URL'))
        self.assertEqual(service['parse_schedule_with_hunyuan']('test')['schedules'],[])
        self.assertEqual(service['requests'].calls,[])

    def test_chat_uses_the_explicitly_configured_endpoint(self):
        service=load_service_functions()
        calls=[]
        class FakeResponse:
            status_code=200
            def json(self):
                return {'choices':[{'message':{'content':'test reply'}}]}
        class FakeRequests:
            def post(self,url,**kwargs):
                calls.append((url,kwargs))
                return FakeResponse()
        service.update(TENCENT_CHAT_URL='https://example.invalid/chat',requests=FakeRequests(),datetime=datetime,
                       get_saved_config=lambda:{'alarms':{'早上':{'h':8,'m':0},'中午':{'h':12,'m':0},'晚上':{'h':20,'m':0}}})
        self.assertEqual(service['chat_with_hunyuan']('test'),'test reply')
        self.assertEqual(calls[0][0],'https://example.invalid/chat')
        self.assertEqual(calls[0][1]['headers']['Authorization'],'Bearer test-key')

if __name__=='__main__':
    unittest.main()
