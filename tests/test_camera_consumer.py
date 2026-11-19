import asyncio
import base64
import sys
import types
from unittest import TestCase
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode

from django.core.exceptions import PermissionDenied

from live.consumers import CameraConsumer, update_camera


class CameraConsumerTests(TestCase):
    def setUp(self):
        self.camera = MagicMock()
        self.camera.user.profile.vendor = True
        self.camera.broadcast_youtube = False
        self.camera_model = MagicMock()
        self.camera_model.objects.filter.return_value.order_by.return_value.first.return_value = self.camera
        self.segment = MagicMock(confirmation_id='id1')
        self.save = MagicMock(return_value=self.segment)
        self.modules = patch.dict(sys.modules, {'live.models': types.SimpleNamespace(VideoCamera=self.camera_model)})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.save_patch = patch('live.video_upload.save_video_segment', self.save)
        self.save_patch.start()
        self.addCleanup(self.save_patch.stop)

    def payload(self):
        return urlencode({
            'frame': 'data:video/webm;base64,' + base64.b64encode(b'video').decode(),
            'confirmation_id': 'id1', 'viduid': 'stream1', 'timestamp': '2026-01-01T00:00:00Z',
        })

    def test_named_fields_and_owner_are_used(self):
        result = update_camera.func(MagicMock(), 12, 'owner', 'private', self.payload(), 'key')
        self.assertEqual(result, 'id1')
        self.camera_model.objects.filter.assert_called_once_with(
            user__id=12, user__profile__name='owner', name='private', key='key'
        )
        self.save.assert_called_once_with(self.camera, '2026-01-01T00:00:00Z', 'stream1', 'id1', b'video')

    def test_invalid_payload_and_unauthorized_camera(self):
        with self.assertRaises(ValueError):
            update_camera.func(MagicMock(), 12, 'owner', 'private', 'frame=bad', 'key')
        self.save.assert_not_called()
        self.camera_model.objects.filter.return_value.order_by.return_value.first.return_value = None
        with self.assertRaises(PermissionDenied):
            update_camera.func(MagicMock(), 12, 'owner', 'private', self.payload(), 'key')

    def test_receive_reports_failures(self):
        consumer = CameraConsumer()
        consumer.user_id = 12
        consumer.camera_user = 'owner'
        consumer.camera_name = 'private'
        consumer.key = 'key'
        consumer.send = AsyncMock()
        consumer.close = AsyncMock()
        with patch('live.consumers.update_camera', AsyncMock(side_effect=ValueError)):
            asyncio.run(consumer.receive(text_data='bad'))
        self.assertIn('Invalid video segment', consumer.send.await_args.kwargs['text_data'])
        with patch('live.consumers.update_camera', AsyncMock(side_effect=PermissionDenied)):
            asyncio.run(consumer.receive(text_data='bad'))
        consumer.close.assert_awaited_once_with(code=4403)
