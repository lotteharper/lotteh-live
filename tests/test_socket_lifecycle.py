import asyncio
import base64
import sys
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode

from channels.testing import WebsocketCommunicator

from live.consumers import CameraConsumer, RemoteConsumer, VideoConsumer


def communicator(consumer, path='/'):
    socket = WebsocketCommunicator(consumer.as_asgi(), path)
    socket.scope['url_route'] = {'kwargs': {'username': 'owner', 'name': 'private'}}
    socket.scope['user'] = SimpleNamespace(id=12)
    socket.scope['session'] = SimpleNamespace(session_key='session')
    return socket


class SocketLifecycleTests(IsolatedAsyncioTestCase):
    async def test_large_video_is_received_as_bounded_chunks(self):
        socket = communicator(CameraConsumer, '/?key=key')
        segment = b'x' * (2 * 1024 * 1024 + 17)
        with patch('live.consumers.get_auth', AsyncMock(return_value=True)), \
                patch('live.consumers.camera_upload_authorized', AsyncMock(return_value=True)), \
                patch('live.consumers.update_camera_binary', AsyncMock(side_effect=lambda *args: args[-2])) as save:
            self.assertTrue((await socket.connect())[0])
            await socket.send_json_to({
                'type': 'segment_start', 'timestamp': '2026-01-01T00:00:00Z',
                'viduid': 'stream1', 'confirmation_id': 'segment1', 'size': len(segment),
            })
            self.assertEqual(await socket.receive_json_from(), {'ready': 'segment1'})
            for offset in range(0, len(segment), 65536):
                await socket.send_to(bytes_data=segment[offset:offset + 65536])
            await socket.send_json_to({'type': 'segment_end'})
            self.assertEqual(await socket.receive_json_from(), {'confirmation_id': 'segment1'})
            self.assertEqual(save.await_args.args[-1], segment)
            await socket.send_json_to({
                'type': 'segment_start', 'timestamp': '2026-01-01T00:00:00Z',
                'viduid': 'stream1', 'confirmation_id': 'segment2', 'size': 1,
            })
            self.assertEqual(await socket.receive_json_from(), {'ready': 'segment2'})
            await socket.send_to(bytes_data=b'x')
            await socket.send_json_to({'type': 'segment_end'})
            self.assertEqual(await socket.receive_json_from(), {'confirmation_id': 'segment2'})
            await socket.disconnect()

    async def test_incomplete_chunked_upload_is_rejected_without_disconnect(self):
        socket = communicator(CameraConsumer, '/?key=key')
        with patch('live.consumers.get_auth', AsyncMock(return_value=True)), \
                patch('live.consumers.camera_upload_authorized', AsyncMock(return_value=True)):
            self.assertTrue((await socket.connect())[0])
            await socket.send_json_to({
                'type': 'segment_start', 'timestamp': '2026-01-01T00:00:00Z',
                'viduid': 'stream1', 'confirmation_id': 'segment1', 'size': 2,
            })
            self.assertEqual(await socket.receive_json_from(), {'ready': 'segment1'})
            await socket.send_to(bytes_data=b'x')
            await socket.send_json_to({'type': 'segment_end'})
            self.assertEqual(await socket.receive_json_from(), {'error': 'Invalid video segment'})
            await socket.send_json_to({
                'type': 'segment_start', 'timestamp': '2026-01-01T00:00:00Z',
                'viduid': 'stream1', 'confirmation_id': 'segment2', 'size': 1,
            })
            self.assertEqual(await socket.receive_json_from(), {'ready': 'segment2'})
            await socket.disconnect()

    async def test_chunked_upload_rejects_unauthorized_camera(self):
        socket = communicator(CameraConsumer, '/?key=key')
        with patch('live.consumers.get_auth', AsyncMock(return_value=True)), \
                patch('live.consumers.camera_upload_authorized', AsyncMock(return_value=False)):
            self.assertTrue((await socket.connect())[0])
            await socket.send_json_to({
                'type': 'segment_start', 'timestamp': '2026-01-01T00:00:00Z',
                'viduid': 'stream1', 'confirmation_id': 'segment1', 'size': 1,
            })
            self.assertEqual(await socket.receive_output(), {'type': 'websocket.close', 'code': 4403})
            await socket.wait()

    async def test_chunked_upload_rejects_oversized_segment(self):
        socket = communicator(CameraConsumer, '/?key=key')
        with patch('live.consumers.get_auth', AsyncMock(return_value=True)), \
                patch('live.consumers.camera_upload_authorized', AsyncMock()) as authorized:
            self.assertTrue((await socket.connect())[0])
            await socket.send_json_to({
                'type': 'segment_start', 'timestamp': '2026-01-01T00:00:00Z',
                'viduid': 'stream1', 'confirmation_id': 'segment1', 'size': 256 * 1024 * 1024 + 1,
            })
            self.assertEqual(await socket.receive_json_from(), {'error': 'Invalid video segment'})
            authorized.assert_not_awaited()
            await socket.disconnect()

    async def test_real_upload_parser_accepts_consecutive_video_segments(self):
        socket = communicator(CameraConsumer, '/?key=key')
        camera = MagicMock()
        camera.user.profile.vendor = True
        camera.broadcast_youtube = False
        model = MagicMock()
        model.objects.filter.return_value.order_by.return_value.first.return_value = camera
        saved = MagicMock()
        saved.side_effect = lambda camera, timestamp, video_id, confirmation_id, content: SimpleNamespace(
            confirmation_id=confirmation_id
        )
        with patch.dict(sys.modules, {'live.models': SimpleNamespace(VideoCamera=model)}), \
                patch('live.consumers.get_auth', AsyncMock(return_value=True)), \
                patch('live.video_upload.save_video_segment', saved):
            self.assertTrue((await socket.connect())[0])
            for confirmation_id in ('one', 'two'):
                payload = urlencode({
                    'timestamp': '2026-01-01T00:00:00Z',
                    'viduid': 'stream1',
                    'confirmation_id': confirmation_id,
                    'frame': 'data:video/webm;base64,' + base64.b64encode(b'video').decode(),
                })
                await socket.send_to(text_data=payload)
                self.assertEqual(await socket.receive_json_from(), {'confirmation_id': confirmation_id})
            self.assertEqual(saved.call_count, 2)
            await socket.disconnect()

    async def test_camera_receives_multiple_segments_and_survives_bad_input(self):
        socket = communicator(CameraConsumer, '/?key=key')
        with patch('live.consumers.get_auth', AsyncMock(return_value=True)), \
                patch('live.consumers.update_camera', AsyncMock(side_effect=['one', ValueError(), 'two'])):
            connected, _ = await socket.connect()
            self.assertTrue(connected)
            await socket.send_to(text_data='first')
            self.assertEqual(await socket.receive_json_from(), {'confirmation_id': 'one'})
            await socket.send_to(text_data='invalid')
            self.assertEqual(await socket.receive_json_from(), {'error': 'Invalid video segment'})
            await socket.send_to(text_data='second')
            self.assertEqual(await socket.receive_json_from(), {'confirmation_id': 'two'})
            await socket.disconnect()

    async def test_remote_accepts_messages_and_disconnects_while_polling(self):
        socket = communicator(RemoteConsumer)
        with patch('live.consumers.get_camera_status', AsyncMock(return_value='y,y,n')):
            connected, _ = await socket.connect()
            self.assertTrue(connected)
            self.assertEqual(await socket.receive_from(), 'y,y,n')
            await socket.send_to(text_data='ping')
            await asyncio.wait_for(socket.disconnect(), timeout=1)

    async def test_viewer_waits_for_first_frame_and_can_disconnect(self):
        socket = communicator(VideoConsumer, '/?index=0')
        with patch('live.consumers.get_user', AsyncMock(return_value=True)), \
                patch('live.consumers.get_camera_data', AsyncMock(side_effect=[None, '/frame.webm'])), \
                patch('live.consumers.settings.LIVE_INTERVAL', 1):
            connected, _ = await socket.connect()
            self.assertTrue(connected)
            self.assertEqual(await socket.receive_from(), '/frame.webm')
            await asyncio.wait_for(socket.disconnect(), timeout=1)

    async def test_viewer_rejects_bad_index(self):
        socket = communicator(VideoConsumer, '/?index=invalid')
        with patch('live.consumers.get_user', AsyncMock(return_value=True)):
            self.assertEqual(await socket.connect(), (False, 4400))
            await socket.wait()
