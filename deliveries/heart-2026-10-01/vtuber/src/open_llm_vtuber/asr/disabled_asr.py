"""Text-only mode: microphone audio is not transcribed or uploaded."""

import numpy as np

from .asr_interface import ASRInterface


class VoiceRecognition(ASRInterface):
    def transcribe_np(self, audio: np.ndarray) -> str:
        return ""
