import os
import sys
import tempfile
import types
import zipfile
from contextlib import nullcontext
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from live.process import _process_live, _process_recording, process_live, process_recording


@override_settings(LIVE_INTERVAL=5000)
class ProcessingTests(SimpleTestCase):
    def setUp(self):
        self.recording = MagicMock(processing=False, processed=False)
        self.recording.last_frame = timezone.now() - timedelta(seconds=60)
        self.recording_model = MagicMock()
        self.recording_model.objects.select_for_update.return_value.get.return_value = self.recording
        self.frame = MagicMock(processed=False)
        self.frame_model = MagicMock()
        self.frame_model.objects.select_for_update.return_value.get.return_value = self.frame
        self.recording_task = MagicMock()
        self.live_task = MagicMock()
        modules = patch.dict(sys.modules, {
            'live.models': types.SimpleNamespace(VideoRecording=self.recording_model, VideoFrame=self.frame_model),
            'lotteh': types.ModuleType('lotteh'),
            'lotteh.celery': types.SimpleNamespace(process_recording=self.recording_task, process_live=self.live_task),
        })
        atomic = patch('django.db.transaction.atomic', return_value=nullcontext())
        on_commit = patch('django.db.transaction.on_commit', side_effect=lambda callback: callback())
        for item in (modules, atomic, on_commit):
            item.start()
            self.addCleanup(item.stop)

    def test_recent_recording_is_rescheduled(self):
        now = timezone.now()
        self.recording.last_frame = now - timedelta(seconds=10)
        with patch('django.utils.timezone.now', return_value=now), patch('live.process._process_recording') as process:
            process_recording(1)
        self.recording_task.apply_async.assert_called_once_with([1], countdown=30)
        process.assert_not_called()
        self.assertFalse(self.recording.processing)

    def test_finished_or_claimed_recording_is_not_processed_twice(self):
        with patch('live.process._process_recording') as process:
            self.recording.processing = True
            process_recording(1)
            self.recording.processing = False
            self.recording.processed = True
            process_recording(1)
        process.assert_not_called()
        self.recording_task.apply_async.assert_not_called()

    def test_failed_recording_is_released_and_retried(self):
        with patch('live.process._process_recording', side_effect=RuntimeError('failed')):
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                process_recording(1)
        self.recording_model.objects.filter.assert_called_once_with(id=1, processed=False)
        self.recording_model.objects.filter.return_value.update.assert_called_once_with(processing=False)
        self.recording_task.apply_async.assert_called_once_with([1], countdown=40)

    def test_frame_is_processed_once_and_failures_are_retried(self):
        with patch('live.process._process_live', side_effect=RuntimeError('failed')) as process:
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                process_live(1, 2)
            self.live_task.apply_async.assert_called_once_with([1, 2], countdown=40)
            self.frame.processed = True
            process_live(1, 2)
            process.assert_called_once_with(1, self.frame)

    def test_recording_is_only_completed_after_concat_and_upload(self):
        camera = MagicMock(upload=True, bucket=False)
        camera_model = MagicMock()
        camera_model.objects.filter.return_value.order_by.return_value.first.return_value = camera
        self.recording_model.objects.get.return_value = self.recording
        self.recording.frames.filter.side_effect = lambda **kwargs: (
            [] if 'processed' in kwargs else MagicMock(count=MagicMock(return_value=0))
        )
        self.recording.frames.all.return_value = []
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'video.mp4')
            with open(path, 'wb') as video:
                video.write(b'video')
            result = types.SimpleNamespace(path=path)
            concat = MagicMock(return_value=result)
            upload = MagicMock(side_effect=RuntimeError('upload failed'))
            modules = {
                'live.models': types.SimpleNamespace(VideoRecording=self.recording_model, VideoCamera=camera_model, get_file_path=lambda *args: 'video.mp4'),
                'live.concat': types.SimpleNamespace(concat=concat),
                'live.duration': types.SimpleNamespace(get_duration=lambda path: 1),
                'audio': types.ModuleType('audio'),
                'audio.transcription': types.SimpleNamespace(get_transcript=lambda path: ('', '')),
                'shell': types.ModuleType('shell'),
                'shell.execute': types.SimpleNamespace(run_command=MagicMock()),
                'live.upload': types.SimpleNamespace(upload_recording=upload),
            }
            self.recording.uploaded = False
            with patch.dict(sys.modules, modules), override_settings(BASE_DIR=directory):
                concat.return_value = None
                with self.assertRaisesRegex(RuntimeError, 'concatenation failed'):
                    _process_recording(self.recording)
                self.assertFalse(self.recording.processed)
                concat.return_value = result
                with self.assertRaisesRegex(RuntimeError, 'upload failed'):
                    _process_recording(self.recording)
                self.assertFalse(self.recording.processed)
                upload.side_effect = None
                _process_recording(self.recording)
            self.assertTrue(self.recording.processed)
            self.assertFalse(self.recording.processing)
            self.recording.frames.clear.assert_called_once()


class CompressedFrameTests(SimpleTestCase):
    def test_compressed_mp4_is_extracted_and_processed(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = os.path.join(directory, 'source.zip')
            output = os.path.join(directory, 'output.mp4')
            with zipfile.ZipFile(archive, 'w') as zipped:
                zipped.writestr('frame.mp4', b'video')

            class Frame:
                compressed = True
                processed = False
                safe = True
                contains_speech = False
                animate_video = False
                recording = MagicMock()

                def __init__(self):
                    self.frame = archive

                def __setattr__(self, name, value):
                    if name == 'frame' and isinstance(value, str):
                        value = types.SimpleNamespace(path=value, name=value)
                    super().__setattr__(name, value)

                def save(self):
                    pass

            frame = Frame()
            frame.recording.first.return_value = None
            camera = MagicMock(mimetype='mp4; codecs="avc1"', server_moderation=False,
                               speech_only=False, censor_audio=False)
            camera_model = MagicMock()
            camera_model.objects.get.return_value = camera
            modules = {
                'live.models': types.SimpleNamespace(VideoCamera=camera_model, get_file_path=lambda *args: 'output.mp4'),
                'shell': types.ModuleType('shell'),
                'shell.execute': types.SimpleNamespace(run_command=MagicMock()),
                'better_profanity': types.SimpleNamespace(profanity=MagicMock()),
            }
            with patch.dict(sys.modules, modules), override_settings(
                MEDIA_ROOT=directory, NUDITY_CENSOR=False, NUDITY_FILTER=False,
                VIDEO_SEGMENTS_PER_MODERATION=10,
            ):
                _process_live(1, frame)
            self.assertTrue(frame.processed)
            self.assertFalse(frame.compressed)
            self.assertFalse(os.path.exists(archive))
            with open(output, 'rb') as video:
                self.assertEqual(video.read(), b'video')
