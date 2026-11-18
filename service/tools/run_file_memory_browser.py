import http.cookiejar
import json
import os
import pathlib
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

root = pathlib.Path(__file__).resolve().parents[1]
folder = pathlib.Path(tempfile.mkdtemp(prefix='deskbot-file-memory-browser-'))
app = '''import os
from pathlib import Path

def create_app():
    from deskbot_server.service import device_data_paths
    from deskbot_server.utils import paths
    data=Path(os.environ['DESKBOT_DB_PATH']).parent/'runtime-data'
    data.mkdir(exist_ok=True)
    device_data_paths.DATA_DIR=data
    paths.DATA_DIR=data
    from deskbot_server.db import init_database
    init_database()
    from deskbot_server.service.user_service import UserService
    from deskbot_server.dao import device_mapper
    from deskbot_server.service import memory_service as memory
    svc=UserService()
    try:
        user=svc.register('browser-memory@example.com','browser-password-123')
        device_mapper.insert('memory-pk','memory-browser-device',user.id,'Memory Test')
        p=memory.ensure_person('memory-browser-device','分页人物')
        for i in range(55):
            memory.add_memory('分页资料'+str(i),device_id='memory-browser-device',person_id=p['id'])
    except ValueError:
        pass
    from tests.sop_browser_app import create_app as create
    return create()
'''
(folder/'memory_browser_app.py').write_text(app)
with socket.socket() as sock:
    sock.bind(('127.0.0.1',0))
    port=sock.getsockname()[1]
base=f'http://127.0.0.1:{port}'
env=dict(os.environ,DESKBOT_DB_PATH=str(folder/'test.db'),DESKBOT_WEB_SECRET_KEY='memory-browser-only',
         PYTHONPATH=os.pathsep.join([str(folder),str(root/'src'),str(root)]),
         DESKBOT_BROWSER_URL=base,
         PLAYWRIGHT_MODULE=os.environ.get('PLAYWRIGHT_MODULE','playwright-core'),
         CHROME_PATH=os.environ.get('CHROME_PATH',''))
command=[str(root/'.venv/bin/python'),'-m','uvicorn','memory_browser_app:create_app','--factory','--host','127.0.0.1','--port',str(port)]
with (folder/'server.log').open('w+') as log:
    server=None
    def start():
        server=subprocess.Popen(command,cwd=root,env=env,stdout=log,stderr=log)
        for _ in range(100):
            try:
                with urllib.request.urlopen(base+'/health',timeout=1) as response:
                    assert json.load(response)=={'status':'ok'}
                    assert response.headers.get('X-Request-ID')
                    return server
            except OSError:
                if server.poll() is not None:
                    log.seek(0)
                    raise RuntimeError(log.read()[-3000:]) from None
                time.sleep(.1)
        raise RuntimeError('startup timeout')
    try:
        server=start()
        subprocess.run(['node','tools/check_file_memory.cjs'],cwd=root,env=env,check=True,timeout=120)
        server.terminate()
        server.wait(timeout=10)
        server=start()
        cookies=http.cookiejar.CookieJar()
        opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
        body=urllib.parse.urlencode({'email':'browser-memory@example.com','password':'browser-password-123'}).encode()
        with opener.open(urllib.request.Request(base+'/login',data=body,headers={'Accept':'application/json'})) as response:
            assert json.load(response)['redirect']=='/home'
        with opener.open(base+'/app/api/memories?device_id=memory-browser-device&query='+urllib.parse.quote('重试验证')) as response:
            assert json.load(response)['total']==1
        print('PASS real HTTP startup, graceful stop, restart, login and persisted file fact')
        print('Browser evidence directory:',folder)
    finally:
        if server and server.poll() is None:
            server.terminate()
            server.wait(timeout=10)
