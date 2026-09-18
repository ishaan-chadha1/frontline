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


class GeminiASR:
    """Gemini transcribing audio directly.

    Measured against the alternatives on code-mixed showroom speech this wins on
    the metric that decides it -- entity accuracy. Numerals and brand names come
    back intact, and feeding the taxonomy's brand list as a biasing hint fixes
    the one failure mode that matters: an unfamiliar brand heard as a familiar
    one (BGauss transcribed as "Bounce" until the list was supplied).

    Two prompt details are load-bearing. "Write every number as digits" -- without
    it the model spells them out and every price becomes unparseable. And the
    brand list, which is what turns a near-miss into a match.

    It also sidesteps the region problem: chirp_2, the only Google STT model that
    handles code-mixing well, is not offered in asia-south1.
    """

    name = "gemini-asr"
    ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    PROMPT = (
        "Transcribe this voice note from an Indian showroom salesperson, verbatim.\n"
        "It is code-mixed Hindi and English. Write Hindi words in Roman script "
        "exactly as spoken -- do not translate.\n"
        "Write every number as digits, never words: 4,500 not four thousand five hundred.\n"
        "{vocab}"
        "If the audio is silent or unintelligible, output nothing at all.\n"
        "Output only the transcript, with no preamble."
    )

    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or os.getenv("ASR_MODEL", os.getenv("GEMINI_MODEL", "gemini-3.8-flash"))
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")

    def transcribe(self, audio: bytes, mime_type: str,
                   vocabulary: list[str] | None = None) -> TranscriptResult:
        import base64
        import json
        import ssl
        import urllib.error
        import urllib.request

        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        vocab = ""
        if vocabulary:
            names = ", ".join(sorted(set(vocabulary)))
            vocab = (f"Brand and model names likely to appear: {names}. "
                     "Match against this list when a name sounds close to one of them.\n")

        body = {
            "contents": [{"role": "user", "parts": [
                {"text": self.PROMPT.format(vocab=vocab)},
                {"inline_data": {"mime_type": mime_type or "audio/webm",
                                 "data": base64.standard_b64encode(audio).decode()}},
            ]}],
            "generationConfig": {"temperature": 0},
        }
        try:
            import certifi

            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ctx = ssl.create_default_context()
        req = urllib.request.Request(
            self.ENDPOINT.format(model=self.model) + f"?key={self.api_key}",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180, context=ctx) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Gemini ASR {exc.code}: {exc.read().decode()[:300]}") from exc

        parts = payload["candidates"][0]["content"].get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()
        return TranscriptResult(text=text, language="hi-en", engine=self.name,
                                engine_version=self.model)


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
    name = (provider or os.getenv("FRONTLINE_ASR", "gemini")).lower()
    if name in ("gemini", "gemini-asr"):
        return GeminiASR()
    if name in ("google", "google-stt", "chirp"):
        return GoogleSTT()
    return StubASR()
