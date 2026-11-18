"""Real Flecket/ASR/VPR integration using synthetic speech and an isolated device.

Runs on the deployed server with its existing configuration. ROM playback is
simulated; no microphone, camera or physical actuator is used. Test user/device
records are removed after verification, while the JSON report is retained.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import time
import traceback
import uuid
import wave
from collections import Counter
from pathlib import Path

import httpx

from deskbot_server.client.flecket_live import FlecketLiveClient
from deskbot_server.config import configure_runtime, load_config
from deskbot_server.dao.memory_file_store import memory_dir
from deskbot_server.db.engine import init_engine
from deskbot_server.model.pb_seq import PbType
from deskbot_server.service import memory_service as memory
from deskbot_server.service.application.voice_snapshot_cache import get_voice_snapshot
from deskbot_server.service.conversation_service import flecket_config
from deskbot_server.service.device_data_paths import safe_device_data_dir
from deskbot_server.service.device_data_service import clear_device_data
from deskbot_server.service.device_identity_service import ensure_device_identity
from deskbot_server.service.device_ws_service import DeviceWsService, _DeviceEntry
from deskbot_server.service.moss_live_service import FlecketBridge
from deskbot_server.service.user_service import UserService
from deskbot_server.service.voiceprint_service import VoiceprintService, build_voiceprint_runtime
from deskbot_server.utils.env import load_dotenv


class Rom:
    closed = False

    async def close(self, **kwargs):
        self.closed = True


class Client(FlecketLiveClient):
    def __init__(self, config):
        super().__init__(config)
        self.inputs, self.answers, self.calls, self.contexts = [], [], [], []
        self.call_names = {}
        self.completed_playbacks = 0
        self.events = Counter()

    async def _receive(self):
        event = await super()._receive()
        self.events[event.get('type', 'unknown')] += 1
        if event.get('type') == 'response.done':
            for item in event['response'].get('output', []):
                if item.get('type') == 'function_call':
                    self.call_names[item['call_id']] = item['name']
        return event

    async def _send(self, event):
        if event.get('type') == 'session.update' and 'tools' in event.get('session', {}):
            event['session']['tools'] = self.agent_tools
        if event.get('type') == 'conversation.item.create' and event.get('item', {}).get('type') == 'function_call_output':
            value = event['item']
            self.calls.append({'name': self.call_names.get(value['call_id']), 'output': json.loads(value['output'])})
        if event.get('type') == 'response.create':
            instructions = event.get('response', {}).get('instructions', '')
            self.contexts.append({'lemon_loaded': '柠檬茶' in instructions, 'osmanthus_loaded': '桂花茶' in instructions})
        await super()._send(event)

    async def receive(self):
        event = await super().receive()
        if event.action == 'input.transcript.done':
            self.inputs.append(event.data)
        if event.action == 'output.transcript.done':
            self.answers.append(event.data['text'])
        return event

    async def send(self, value):
        await super().send(value)
        if value.get('action') == 'playback.completed':
            self.completed_playbacks += 1


class Harness:
    def __init__(self, device):
        self.device = device
        self.bridge = self.client = self.hub = None
        self.workers = []

    async def connect(self):
        if self.bridge:
            await self.close()
        DeviceWsService.reset_instance()
        self.hub = DeviceWsService()
        self.rom = Rom()
        entry = _DeviceEntry(device_id=self.device, ws=self.rom, generation=1,
                             capabilities={'pb_stream_v1', 'pb_playback_progress_v1'})
        self.hub._devices[self.device] = entry
        self.client = Client(flecket_config(self.device))
        self.bridge = FlecketBridge(self.hub, self.device, self.rom, client=self.client)
        entry.conversation_session = self.bridge
        permitted = {'chat_search', 'chat_read', 'person_read', 'person_update', 'memory_forget',
                     'register_voiceprint', 'recognize_voiceprint'}
        self.client.agent_tools = [t for t in self.client.agent_tools if t['name'] in permitted]
        self.client.robot_tool = None
        self.client.performance_handler = None
        self.client.robot_instructions = ''
        playback = asyncio.Queue()
        cancelled, played = set(), {}

        async def downlink(device_id, block, **kwargs):
            ack = {'type': 'pb_ack', 'req': block.req, 'epoch': block.epoch, 'idx': block.idx}
            if block.type == PbType.CANCEL:
                cancelled.add(block.req)
                self.bridge.offer_ack({**ack, 'ack_type': 'pb_cancelled', 'played_ms': played.get(block.req, 0)})
            else:
                playback.put_nowait(block)
                if block.type != PbType.END:
                    entry.ack_queue.put_nowait({**ack, 'ack_type': 'pb_credit'})
            return True

        async def play():
            while True:
                block = await playback.get()
                if block.req in cancelled:
                    continue
                ack = {'type': 'pb_ack', 'req': block.req, 'epoch': block.epoch, 'idx': block.idx}
                if block.type == PbType.END:
                    entry.ack_queue.put_nowait({**ack, 'ack_type': 'pb_end'})
                else:
                    await asyncio.sleep(block.chunk_ms / 1000)
                    if block.req in cancelled:
                        continue
                    played[block.req] = block.pts_ms + block.chunk_ms
                    self.bridge.offer_ack({**ack, 'ack_type': 'pb_progress', 'played_ms': played[block.req]})

        self.hub._do_send_to_device = downlink
        self.workers = [asyncio.create_task(self.hub._device_loop(self.device, 1)), asyncio.create_task(play())]
        await self.bridge.start()

    async def say(self, key, audio, timeout=80):
        await asyncio.sleep(.7)
        before_input, before_answer = len(self.client.inputs), len(self.client.answers)
        before_call, before_context = len(self.client.calls), len(self.client.contexts)
        before_playback = self.client.completed_playbacks
        with wave.open(str(audio / (key + '.wav'))) as source:
            assert (source.getnchannels(), source.getframerate(), source.getsampwidth()) == (1, 16000, 2)
            pcm = source.readframes(source.getnframes())
        start = time.monotonic()
        for offset in range(0, len(pcm) + 32000, 640):
            if self.rom.closed:
                break
            self.bridge.offer_audio({'codec': 's16le', 'sr': 16000, 'ch': 1}, pcm[offset:offset+640].ljust(640, b'\0'))
            await asyncio.sleep(.02)
        reset = False
        async with asyncio.timeout(timeout):
            while True:
                calls = self.client.calls[before_call:]
                reset = any(c['output'].get('reset_session') for c in calls)
                if reset or self.client.completed_playbacks > before_playback:
                    break
                if self.rom.closed:
                    raise RuntimeError('upstream closed before verified response')
                await asyncio.sleep(.05)
        await asyncio.sleep(.2)
        await self.bridge.memory.flush()
        result = {'stage': key, 'input': [v['text'] for v in self.client.inputs[before_input:]],
                  'answers': self.client.answers[before_answer:], 'tools': self.client.calls[before_call:],
                  'context': self.client.contexts[before_context:], 'voice_state': (get_voice_snapshot(self.device) or {}).get('state'),
                  'playback_completed': self.client.completed_playbacks > before_playback,
                  'session_reset': reset, 'elapsed_s': round(time.monotonic()-start, 2)}
        print(json.dumps({'stage': key, 'input': result['input'], 'tools': [c['name'] for c in result['tools']],
                          'voice_state': result['voice_state'], 'answers': result['answers'], 'session_reset': reset}, ensure_ascii=False), flush=True)
        return result

    async def close(self):
        if self.bridge:
            await self.bridge.close()
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers = []
        if self.hub:
            await self.hub.shutdown()
        self.bridge = None


async def verify(args):
    for key in ('enroll', 'remember', 'episode', 'load', 'recall', 'correct', 'forget', 'after_forget'):
        with wave.open(str(args.audio / (key + '.wav'))) as source:
            if source.getnframes() < 16000:
                raise ValueError('Synthetic audio missing or incomplete: ' + key)
    load_dotenv()
    config = load_config('config.yaml')
    configure_runtime(config, 'config.yaml')
    voice_runtime = build_voiceprint_runtime(config)
    VoiceprintService().configure(voice_runtime)
    assert VoiceprintService().enabled(), 'Real voiceprint service must be enabled'
    # The main HTTP health endpoint becomes ready before model warm-up ends.
    # Check the actual inference dependency before enrolling any fixture.
    async with httpx.AsyncClient(timeout=3) as http:
        for _attempt in range(40):
            try:
                ready = await http.get(voice_runtime.external_url.rstrip('/') + '/health')
                if ready.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.5)
        else:
            raise RuntimeError('Voiceprint dependency did not become healthy')
    stamp = uuid.uuid4().hex[:12]
    device = 'memory_live_' + stamp
    ensure_device_identity(device)
    harness = Harness(device)
    result = {'kind': 'real_flecket_asr_vpr_with_synthetic_speech', 'device_id': device,
              'real_agent': True, 'real_asr': True, 'real_voiceprint': True, 'physical_rom': False,
              'microphone_capture': False, 'camera_capture': False, 'stages': [], 'checks': {}}
    uid = None
    try:
        await harness.connect()
        # Own test account allows actual production HTTP reads without using any
        # existing person's login. No message is sent to the synthetic address.
        password = 'test-only-' + uuid.uuid4().hex
        user = UserService().register('memory-' + stamp + '@example.invalid', password)
        uid = user.id
        UserService().bind_device(uid, device)
        enroll = await harness.say('enroll', args.audio)
        result['stages'].append(enroll)
        assert any(c['name'] == 'register_voiceprint' and c['output'].get('ok') for c in enroll['tools']), 'voice enrollment not executed'
        remembered = await harness.say('remember', args.audio)
        result['stages'].append(remembered)
        entries = memory.list_memory_for_device(device)
        assert any('柠檬茶' in e['text'] for e in entries), 'preference was not saved'
        original_preference_ids = {e['id'] for e in entries if '柠檬茶' in e['text']}
        result['daily_files'] = [p.name for p in memory_dir(device).glob('chat_*.txt')]
        result['person_files'] = [p.name for p in memory_dir(device).glob('person_*.txt')]
        assert result['daily_files'] and result['person_files'], 'expected memory files missing'
        result['checks']['daily_record_and_person_update'] = True
        episode = await harness.say('episode', args.audio)
        result['stages'].append(episode)
        assert memory.search_chat(device, '盒')['total'] > 0, 'episode not recorded'
        async with httpx.AsyncClient(base_url=args.http_url) as http:
            assert (await http.get('/app/api/memories')).status_code == 401, 'anonymous memory access allowed'
            login = await http.post('/login', data={'email': user.email, 'password': password})
            assert login.status_code in (302, 303), 'test account HTTP login failed'
            for endpoint in ('/app/api/memories', '/app/api/memory/chat'):
                response = await http.get(endpoint, params={'device_id': device})
                assert response.status_code == 200 and response.json()['total'] > 0, 'production HTTP memory read failed'
            denied = await http.get('/app/api/memory/chat', params={'device_id': 'memory_unowned_' + stamp})
            assert denied.status_code == 403, 'cross-device HTTP access allowed'
        result['checks']['production_http_auth_and_file_reads'] = True
        await harness.connect()
        loaded = await harness.say('load', args.audio)
        result['stages'].append(loaded)
        assert loaded['voice_state'] == 'found', 'synthetic voice not recognized'
        assert any(c['lemon_loaded'] for c in loaded['context']), 'person profile absent from prompt'
        assert '柠檬茶' in ''.join(loaded['answers']), 'new session did not recall loaded preference'
        result['checks']['recognized_profile_loaded_across_connection'] = True
        await harness.connect()
        recalled = await harness.say('recall', args.audio)
        result['stages'].append(recalled)
        assert any(c['name'] in {'chat_search', 'chat_read'} and c['output'].get('ok') for c in recalled['tools']), 'history tool not executed'
        assert '绿' in ''.join(recalled['answers']) and '盒' in ''.join(recalled['answers']), 'wrong historical answer'
        result['checks']['history_search'] = True
        corrected = await harness.say('correct', args.audio)
        result['stages'].append(corrected)
        active = memory.list_memory_for_device(device)
        assert any('桂花茶' in e['text'] for e in active), 'new preference missing'
        assert original_preference_ids.isdisjoint(e['id'] for e in active), 'old positive preference still active'
        await harness.connect()
        updated = await harness.say('load', args.audio)
        result['stages'].append({**updated, 'stage': 'load_after_correction'})
        assert '桂花茶' in ''.join(updated['answers']), 'corrected preference not loaded'
        result['checks']['correction_and_reload'] = True
        forgotten = await harness.say('forget', args.audio)
        result['stages'].append(forgotten)
        assert any(c['name'] == 'memory_forget' and c['output'].get('ok') for c in forgotten['tools']), 'forget tool not executed'
        assert memory.search_chat(device, '柠檬茶')['total'] == 0, 'old preference remains in searchable records'
        assert memory.search_chat(device, '桂花茶')['total'] == 0, 'corrected preference remains in searchable records'
        for path in memory_dir(device).iterdir():
            if path.is_file():
                assert not any(word in path.read_text() for word in ('柠檬茶', '桂花茶')), 'forgotten plaintext remains on disk'
        assert memory.search_chat(device, '绿色盒子')['total'] > 0, 'independent historical input was erased'
        await harness.connect()
        after = await harness.say('after_forget', args.audio)
        result['stages'].append(after)
        assert not any(c['lemon_loaded'] or c['osmanthus_loaded'] for c in after['context']), 'forgotten profile reloaded'
        assert not any(word in ''.join(after['answers']) for word in ('柠檬茶', '桂花茶')), 'Agent recovered forgotten preference'
        result['checks']['forget_and_no_resurrection'] = True
        result['status'] = 'passed'
    except Exception as exc:
        result['status'] = 'failed'
        result['error_kind'] = type(exc).__name__
        result['error_location'] = [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
                                    for frame in traceback.extract_tb(exc.__traceback__)]
        result['error'] = str(exc) if isinstance(exc, AssertionError) else 'integration stage failed; inspect controlled server log'
        if harness.client:
            result['incomplete_stage'] = {'inputs': harness.client.inputs, 'answers': harness.client.answers,
                                          'tools': harness.client.calls, 'contexts': harness.client.contexts,
                                          'events': dict(harness.client.events),
                                          'uploaded_frames': harness.bridge.uploaded_frames,
                                          'voice_state': (get_voice_snapshot(device) or {}).get('state')}
    finally:
        await harness.close()
        if not args.keep_fixture:
            cleanup = clear_device_data(device)
            from sqlalchemy import text
            with init_engine().begin() as connection:
                connection.execute(text('DELETE FROM devices WHERE device_id=:device'), {'device': device})
                if uid:
                    connection.execute(text('DELETE FROM users WHERE id=:id'), {'id': uid})
            # clear_device_data preserves a real device's binding/settings and
            # reseeds defaults. This synthetic device is being removed entirely.
            if not cleanup['errors']:
                directory = safe_device_data_dir(device)
                if directory.exists():
                    shutil.rmtree(directory)
            result['fixture_cleanup'] = 'passed' if not cleanup['errors'] else cleanup['errors']
        else:
            result['fixture_cleanup'] = 'kept_for_debug'
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps({'status': result['status'], 'checks': result['checks'], 'report': str(args.report),
                          'error': result.get('error')}, ensure_ascii=False), flush=True)
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--keep-fixture', action='store_true')
    parser.add_argument('--http-url', default='http://127.0.0.1:9000')
    raise SystemExit(asyncio.run(verify(parser.parse_args())))
