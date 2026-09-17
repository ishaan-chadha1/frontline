"""Speech to text, provider-pluggable.

The largest technical risk in the system. The input is not clean dictation --
it is code-mixed Hindi and English mid-sentence, with numerals carrying the
whole meaning and showroom noise underneath.

No provider name appears outside its own adapter, so the Sprint 1 benchmark can
swap engines without touching anything downstream.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class TranscriptResult:
    text: str
    language: str | None = None
    mean_confidence: float | None = None
    engine: str = "unknown"
    engine_version: str = "1"
    segments: list[dict] = field(default_factory=list)


class ASRProvider(Protocol):
    name: str

    def transcribe(self, audio: bytes, mime_type: str,
                   vocabulary: list[str] | None = None) -> TranscriptResult: ...


class GoogleSTT:
    """Google Speech-to-Text v2. Chirp handles Indic code-mixing better than the
    older models and is already enabled on the project."""

    name = "google-stt"

    def __init__(self, project: str | None = None, location: str = "asia-south1"):
        self.project = project or os.getenv("GCP_PROJECT", "")
        self.location = location
        self.model = os.getenv("STT_MODEL", "chirp_2")

    def transcribe(self, audio: bytes, mime_type: str,
                   vocabulary: list[str] | None = None) -> TranscriptResult:
        from google.cloud.speech_v2 import SpeechClient
        from google.cloud.speech_v2.types import cloud_speech
        from google.api_core.client_options import ClientOptions

        client = SpeechClient(
            client_options=ClientOptions(
                api_endpoint=f"{self.location}-speech.googleapis.com"
            )
        )
        features = cloud_speech.RecognitionFeatures(enable_automatic_punctuation=True)
        config = cloud_speech.RecognitionConfig(
            auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
            # Hindi first, English as the alternate: the notes are code-mixed and
            # brand or finance terms arrive in English inside Hindi syntax.
            language_codes=["hi-IN", "en-IN"],
            model=self.model,
            features=features,
        )
        request = cloud_speech.RecognizeRequest(
            recognizer=f"projects/{self.project}/locations/{self.location}/recognizers/_",
            config=config,
            content=audio,
        )
        response = client.recognize(request=request)
        parts, confidences = [], []
        for result in response.results:
            if result.alternatives:
                parts.append(result.alternatives[0].transcript)
                if result.alternatives[0].confidence:
                    confidences.append(result.alternatives[0].confidence)
        return TranscriptResult(
            text=" ".join(p.strip() for p in parts).strip(),
            language="hi-IN",
            mean_confidence=sum(confidences) / len(confidences) if confidences else None,
            engine=self.name,
            engine_version=self.model,
        )


class StubASR:
    name = "stub"

    def transcribe(self, audio: bytes, mime_type: str,
                   vocabulary: list[str] | None = None) -> TranscriptResult:
        return TranscriptResult(
            text="(stub ASR: no transcription available)", engine=self.name
        )


def get_asr(provider: str | None = None) -> ASRProvider:
    name = (provider or os.getenv("FRONTLINE_ASR", "google")).lower()
    if name in ("google", "google-stt", "chirp"):
        return GoogleSTT()
    return StubASR()
