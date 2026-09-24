from __future__ import annotations

import unittest

from aioverse.models import (
	AudioInputSegment,
	AudioUrlSegment,
	TextSegment,
	UserContext,
	VideoInputSegment,
	VideoUrlSegment,
)


class MediaSegmentsTests(unittest.TestCase):

	def test_audio_url_segment_serializes_and_restores(self):
		segment = AudioUrlSegment(
			url="https://example.com/audio.mp3",
			format="mp3",
		)

		self.assertEqual(segment.model_dump(mode="json"), {
			"type": "audio_url",
			"audio_url": {
				"url": "https://example.com/audio.mp3",
				"format": "mp3",
			},
		})
		restored = AudioUrlSegment.model_validate_json(segment.model_dump_json())
		self.assertEqual(restored, segment)

	def test_video_input_segment_serializes_and_restores(self):
		segment = VideoInputSegment(data="dmlkZW8=", format="webm")

		self.assertEqual(segment.model_dump(mode="json"), {
			"type": "input_video",
			"input_video": {
				"data": "dmlkZW8=",
				"format": "webm",
			},
		})
		restored = VideoInputSegment.model_validate_json(segment.model_dump_json())
		self.assertEqual(restored, segment)

	def test_nested_audio_and_video_formats_are_flattened(self):
		audio_url = AudioUrlSegment.model_validate({
			"type": "audio_url",
			"audio_url": {
				"url": "https://example.com/audio.mp3",
				"format": "mp3",
			},
		})
		video_input = VideoInputSegment.model_validate({
			"type": "input_video",
			"input_video": {
				"data": "dmlkZW8=",
				"format": "webm",
			},
		})
		video_url = VideoUrlSegment.model_validate({
			"type": "video_url",
			"video_url": {
				"url": "https://example.com/video.mp4",
				"format": "mp4",
			},
		})

		self.assertEqual(audio_url.url, "https://example.com/audio.mp3")
		self.assertEqual(audio_url.format, "mp3")
		self.assertEqual(video_input.data, "dmlkZW8=")
		self.assertEqual(video_input.format, "webm")
		self.assertEqual(video_url.url, "https://example.com/video.mp4")
		self.assertEqual(video_url.format, "mp4")

	def test_video_url_segment_serializes_and_restores(self):
		segment = VideoUrlSegment(
			url="https://example.com/video.mp4",
			format="mp4",
		)

		self.assertEqual(segment.model_dump(mode="json"), {
			"type": "video_url",
			"video_url": {
				"url": "https://example.com/video.mp4",
				"format": "mp4",
			},
		})
		restored = VideoUrlSegment.model_validate_json(segment.model_dump_json())
		self.assertEqual(restored, segment)

	def test_context_restores_explicit_audio_and_video_segments(self):
		context = UserContext(content=[
			TextSegment(text="process media"),
			AudioInputSegment(data="YXVkaW8=", format="wav"),
			AudioUrlSegment(url="https://example.com/audio.mp3"),
			VideoInputSegment(data="dmlkZW8="),
			VideoUrlSegment(url="https://example.com/video.mp4"),
		])

		restored = UserContext.model_validate_json(context.model_dump_json())

		self.assertEqual(
			[type(segment) for segment in restored.content],
			[
				TextSegment,
				AudioInputSegment,
				AudioUrlSegment,
				VideoInputSegment,
				VideoUrlSegment,
			],
		)
		self.assertEqual(restored.model_dump(mode="json"), context.model_dump(mode="json"))


if __name__ == "__main__":
	unittest.main()
