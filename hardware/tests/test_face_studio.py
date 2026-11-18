"""Test local editor writes without opening a socket or touching real source files."""
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import face_studio


class FaceStudioTest(unittest.TestCase):
    def test_save_validation_conflict_and_origin_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root/'source.json'
            source.write_bytes(face_studio.SOURCE.read_bytes())
            output = root/'generated'
            handler = face_studio.make_handler(source, output, 'session-token')
            doc = json.loads(source.read_bytes())
            rev = hashlib.sha256(source.read_bytes()).hexdigest()

            def request(body, token='session-token', origin='http://127.0.0.1:8975'):
                data = json.dumps(body).encode()
                h = object.__new__(handler)
                h.path='/save'
                h.server=SimpleNamespace(server_port=8975)
                h.headers={'X-Studio-Token':token,'Origin':origin,'Content-Length':str(len(data))}
                h.rfile=io.BytesIO(data)
                replies=[]
                h.reply=lambda *args: replies.append(args)
                with patch.object(face_studio,'ROOT',root):
                    h.do_POST()
                return replies[0]

            body=dict(document=doc,revision=rev)
            self.assertEqual(request(body,token='wrong')[0],403)
            self.assertEqual(request(body,origin='https://unrelated.example')[0],403)
            self.assertEqual(request(dict(document={},revision=rev))[0],400)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),rev)
            doc['emotions'][0]['frames'][0]['ms']+=1
            response=request(body)
            self.assertEqual(response[0],200)
            self.assertEqual(json.loads(source.read_text()),doc)
            self.assertEqual(json.loads((output/'face_assets_manifest.json').read_text())['version'],json.loads(response[1])['version'])
            self.assertTrue((root/'hardware/.pio/face-backups'/f'{rev}.json').exists())
            self.assertEqual(request(body)[0],409)


if __name__=='__main__':
    unittest.main()
