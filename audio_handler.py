import logging
import os
from typing import Awaitable, Callable, Optional

from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents

logger = logging.getLogger(__name__)


class DeepgramSTT:
    """Bridges Twilio mulaw audio to Deepgram real-time STT via async WebSocket."""

    def __init__(
        self,
        on_transcript: Callable[[str, bool], Awaitable[None]],
        on_utterance_end: Callable[[], Awaitable[None]],
    ):
        self._on_transcript = on_transcript
        self._on_utterance_end = on_utterance_end
        self._connection = None
        self._is_connected = False

    async def connect(self) -> None:
        client = DeepgramClient(os.getenv("DEEPGRAM_API_KEY", ""))
        self._connection = client.listen.asyncwebsocket.v("1")

        # Deepgram SDK v3 asyncwebsocket requires async event handlers (awaits them internally).
        async def on_transcript(*args, result=None, **kwargs):
            if result is None:
                return
            try:
                alternatives = result.channel.alternatives
                if not alternatives:
                    return
                transcript = alternatives[0].transcript
                if not transcript:
                    return
                if result.is_final and result.speech_final:
                    await self._on_transcript(transcript, True)
                elif result.is_final:
                    await self._on_transcript(transcript, False)
            except Exception as e:
                logger.error(f"Deepgram transcript handler error: {e}")

        async def on_utterance_end(*args, **kwargs):
            try:
                await self._on_utterance_end()
            except Exception as e:
                logger.error(f"Deepgram utterance_end handler error: {e}")

        async def on_error(*args, error=None, **kwargs):
            logger.error(f"Deepgram error: {error}")

        self._connection.on(LiveTranscriptionEvents.Transcript, on_transcript)
        self._connection.on(LiveTranscriptionEvents.UtteranceEnd, on_utterance_end)
        self._connection.on(LiveTranscriptionEvents.Error, on_error)

        options = LiveOptions(
            model="nova-2-medical",
            encoding="mulaw",
            sample_rate=8000,
            channels=1,
            endpointing=500,
            utterance_end_ms="1200",
            smart_format=True,
            interim_results=True,
        )

        started = await self._connection.start(options)
        if not started:
            raise RuntimeError("Failed to start Deepgram WebSocket connection")
        self._is_connected = True
        logger.info("Deepgram STT connected")

    async def send_audio(self, data: bytes) -> None:
        if self._is_connected and self._connection:
            try:
                await self._connection.send(data)
            except Exception as e:
                logger.error(f"Error sending audio to Deepgram: {e}")

    async def close(self) -> None:
        if self._is_connected:
            self._is_connected = False
            try:
                await self._connection.finish()
                logger.info("Deepgram STT disconnected")
            except Exception as e:
                logger.error(f"Error closing Deepgram connection: {e}")
