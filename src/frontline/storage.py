"""Audio storage, content-addressed.

The original bytes are the bottom of the evidence chain. They are also what
makes early ASR mistakes recoverable: re-transcribing the archive with a better
engine only works if the archive exists.

Content-addressing by SHA-256 means a redelivered webhook or a double-tap on
the record button lands in one place rather than two.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .config import settings

EXT = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/wav": "wav",
       "audio/x-wav": "wav", "audio/mpeg": "mp3", "audio/mp4": "m4a",
       "audio/aac": "aac"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _key(sha: str, mime: str) -> str:
    return f"audio/{sha[:2]}/{sha}.{EXT.get((mime or '').split(';')[0], 'bin')}"


def store(data: bytes, mime_type: str) -> tuple[str, str]:
    """Persist and return (sha256, uri).

    Cloud Storage when a bucket is configured, local disk otherwise, so the
    whole pipeline still runs on a laptop with no cloud credentials.
    """
    sha = digest(data)
    key = _key(sha, mime_type)
    bucket = settings.gcs_audio_bucket or os.getenv("GCS_AUDIO_BUCKET")

    if bucket:
        try:
            from google.cloud import storage as gcs

            client = gcs.Client(project=settings.gcp_project)
            blob = client.bucket(bucket).blob(key)
            if not blob.exists():
                blob.upload_from_string(
                    data, content_type=mime_type or "application/octet-stream")
            return sha, f"gs://{bucket}/{key}"
        except Exception as exc:  # noqa: BLE001
            # Local development has no application credentials; Cloud Run does.
            # Losing the audio would be worse than storing it on disk, so fall
            # back loudly rather than failing the capture.
            print(f"storage: cloud upload unavailable ({type(exc).__name__}), "
                  f"writing to disk", flush=True)

    path = (Path(settings.audio_dir) / key).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(data)
    return sha, path.as_uri()
