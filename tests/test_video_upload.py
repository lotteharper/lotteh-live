import io
import sys
import types
import zipfile
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from live.video_upload import save_video_segment


@override_settings(LIVE_INTERVAL=5000, LIVE_SHORT_SECONDS=10)
class VideoSegmentTests(SimpleTestCase):
    def setUp(self):
        self.camera = MagicMock()
        self.camera.id = 1
        self.camera.name = 'private'
        self.camera.mimetype = 'webm; codecs="vp8,opus"'
        self.camera.compress_video = False
        self.camera.video_length_minutes = 0.25
        self.camera.short_mode = False
        self.camera.recording = False
        self.frame = MagicMock()
        self.frame.id = 2
        self.frame.frame.path = '/tmp/segment.webm'
        self.frame_model = MagicMock(return_value=self.frame)
        self.recording_model = MagicMock()
        self.camera_model = MagicMock()
        self.is_still = MagicMock(return_value=(False, 0.25))
        self.process_live = MagicMock()
        self.process_recording = MagicMock()
        modules = {
            'live.models': types.SimpleNamespace(VideoFrame=self.frame_model, VideoRecording=self.recording_model, VideoCamera=self.camera_model),
            'live.still': types.SimpleNamespace(is_still=self.is_still),
            'lotteh.celery': types.SimpleNamespace(process_live=self.process_live, process_recording=self.process_recording),
        }
        self.modules = patch.dict(sys.modules, modules)
        self.modules.start()
        self.addCleanup(self.modules.stop)
        atomic = patch('live.video_upload.transaction.atomic', return_value=nullcontext())
        on_commit = patch('live.video_upload.transaction.on_commit', side_effect=lambda callback: callback())
        atomic.start()
        on_commit.start()
        self.addCleanup(atomic.stop)
        self.addCleanup(on_commit.stop)

    def save(self, data=b'video'):
        return save_video_segment(self.camera, '2026-01-01T00:00:00Z', 'stream1', 'segment1', data)

    def test_live_segment_without_recording(self):
        self.save()
        self.frame.frame.save.assert_called_once()
        self.process_live.delay.assert_called_once_with(1, 2)
        self.process_recording.apply_async.assert_not_called()
        self.recording_model.objects.select_for_update.assert_not_called()

    def test_recording_rolls_over_at_configured_boundary(self):
        self.camera.recording = True
        previous = MagicMock()
        previous.frames.count.return_value = 3
        self.recording_model.objects.select_for_update.return_value.filter.return_value.order_by.return_value.first.return_value = previous
        new = self.recording_model.objects.create.return_value
        self.save()
        new.frames.add.assert_called_once_with(self.frame)
        previous.frames.add.assert_not_called()
        self.process_recording.apply_async.assert_called_once_with([new.id], countdown=60)

    def test_existing_recording_below_limit_is_reused(self):
        self.camera.recording = True
        previous = MagicMock()
        previous.frames.count.return_value = 2
        self.recording_model.objects.select_for_update.return_value.filter.return_value.order_by.return_value.first.return_value = previous
        self.save()
        previous.frames.add.assert_called_once_with(self.frame)
        self.recording_model.objects.create.assert_not_called()

    def test_short_mode_starts_new_recording_at_short_limit(self):
        self.camera.recording = True
        self.camera.short_mode = True
        previous = MagicMock()
        previous.frames.count.return_value = 2
        self.recording_model.objects.select_for_update.return_value.filter.return_value.order_by.return_value.first.return_value = previous
        self.save()
        self.recording_model.objects.create.return_value.frames.add.assert_called_once_with(self.frame)

    def test_recording_uses_server_upload_time_and_excludes_finished_recordings(self):
        self.camera.recording = True
        previous = MagicMock()
        previous.frames.count.return_value = 1
        self.recording_model.objects.select_for_update.return_value.filter.return_value.order_by.return_value.first.return_value = previous
        with patch('live.video_upload.timezone.now') as now:
            self.save()
        self.assertEqual(previous.last_frame, now.return_value)
        self.recording_model.objects.select_for_update.return_value.filter.assert_called_once_with(
            user=self.camera.user, camera=self.camera.name, camera_id='stream1',
            processing=False, processed=False,
        )

    def test_compressed_segment_is_inspected_as_video(self):
        self.camera.compress_video = True
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as zipped:
            zipped.writestr('frame.webm', b'video')
        def inspect(path):
            with open(path, 'rb') as video:
                self.assertEqual(video.read(), b'video')
            return False, 0.25
        self.is_still.side_effect = inspect
        self.save(archive.getvalue())
        self.is_still.assert_called_once()
        self.frame.frame.save.assert_called_once()
        self.assertEqual(self.frame.frame.save.call_args.args[0], 'frame.zip')

    def test_invalid_metadata_and_archive_are_rejected(self):
        with self.assertRaises(ValueError):
            save_video_segment(self.camera, 'not a timestamp', 'stream1', 'segment1', b'video')
        self.camera.compress_video = True
        with self.assertRaises(ValueError):
            self.save(b'not a zip')
        self.frame.frame.delete.assert_called_once_with(save=False)
