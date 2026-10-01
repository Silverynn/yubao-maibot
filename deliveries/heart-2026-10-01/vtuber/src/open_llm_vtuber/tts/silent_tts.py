"""Text-only output: never transmit reply text to a speech service."""

from .tts_interface import TTSInterface


class TTSEngine(TTSInterface):
    def generate_audio(self, text: str, file_name_no_ext=None):
        return None
