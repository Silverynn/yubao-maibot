import os
from pathlib import Path
from typing import Literal

import requests
from dotenv import dotenv_values
from loguru import logger

from .tts_interface import TTSInterface


class TTSEngine(TTSInterface):
    """Generate speech with the Fish Audio free developer model."""

    file_extension = "wav"

    def __init__(
        self,
        api_key: str,
        reference_id: str,
        latency: Literal["normal", "balanced"] = "balanced",
        base_url: str = "https://api.fish.audio",
    ):
        project_root = Path(__file__).resolve().parents[3]
        local_key = dotenv_values(project_root / ".env").get("FISH_AUDIO_API_KEY")
        self.api_key = api_key or os.environ.get("FISH_AUDIO_API_KEY") or local_key
        if not self.api_key:
            raise ValueError("Fish Audio API key is missing; set FISH_AUDIO_API_KEY in .env")
        if not reference_id:
            raise ValueError("Fish Audio reference_id is missing")

        self.reference_id = reference_id
        self.latency = latency
        self.base_url = base_url.rstrip("/")
        logger.info("Fish TTS initialized with free model and voice {}", reference_id)

    def generate_audio(self, text: str, file_name_no_ext=None):
        file_name = self.generate_cache_file_name(file_name_no_ext, self.file_extension)
        try:
            with requests.post(
                f"{self.base_url}/v1/tts",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "model": "s2.1-pro-free",
                },
                json={
                    "text": text,
                    "reference_id": self.reference_id,
                    "latency": self.latency,
                    "format": self.file_extension,
                },
                stream=True,
                timeout=120,
            ) as response:
                if not response.ok:
                    logger.error("Fish TTS failed with HTTP {}", response.status_code)
                    return None
                with open(file_name, "wb") as audio_file:
                    for chunk in response.iter_content(chunk_size=65536):
                        if chunk:
                            audio_file.write(chunk)
        except (requests.RequestException, OSError) as error:
            logger.error("Fish TTS failed: {}", type(error).__name__)
            return None
        return file_name
