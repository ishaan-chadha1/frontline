"""Extraction: one transcript becomes N structured events.

This is where compaction happens. After this step nothing downstream ever reads
a transcript, which is what makes both a 90-day leadership query and a 300ms
call lookup cheap at the same time.

Provider-pluggable on purpose: the model is a config line, and the eval set
decides which one wins -- not the price list.
"""
from __future__ import annotations

import json
import os
import time
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .config import settings
from .prompts import PROMPT_VERSION, build_system
from .taxonomy import Taxonomy, extraction_schema


@dataclass
class ExtractedEvent:
    node: str
    slots: dict[str, str | None]
    polarity: int
    intensity: int | None
    span: str
    confidence: int


@dataclass
class ExtractionResult:
    events: list[ExtractedEvent]
    interaction_count: int
    unclear: bool
    model: str
    prompt_version: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None


def _ssl_context() -> ssl.SSLContext:
    """macOS system Python ships without a CA bundle; certifi supplies one."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class Extractor(Protocol):
    model: str

    def extract(self, transcript: str, tax: Taxonomy) -> ExtractionResult: ...


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Translate the canonical JSON Schema into Gemini's dialect.

    Gemini takes a subset: uppercase type names, `nullable` instead of a union
    with null, and no `additionalProperties`. Keeping the canonical schema
    provider-neutral and translating at the edge means adding a provider does
    not touch the taxonomy layer.
    """

    def convert(node: dict[str, Any]) -> dict[str, Any]:
        types = node.get("type")
        nullable = False
        if isinstance(types, list):
            nullable = "null" in types
            types = next(t for t in types if t != "null")
        out: dict[str, Any] = {"type": str(types).upper()}
        if nullable:
            out["nullable"] = True
        if "enum" in node:
            values = [v for v in node["enum"] if v is not None]
            # Gemini supports enum on strings only; integers carry it in prose.
            if out["type"] == "STRING":
                out["enum"] = values
        if out["type"] == "ARRAY":
            out["items"] = convert(node["items"])
        if out["type"] == "OBJECT":
            out["properties"] = {k: convert(v) for k, v in node["properties"].items()}
            out["required"] = node.get("required", [])
        return out

    return convert(schema)


class GeminiExtractor:
    """Gemini via the generativelanguage endpoint -- the same surface already
    proven in care-companion."""

    ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or settings.gemini_model
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set (see .env.example)")

    def extract(self, transcript: str, tax: Taxonomy) -> ExtractionResult:
        body = {
            "systemInstruction": {"parts": [{"text": build_system(tax)}]},
            "contents": [{"role": "user", "parts": [{"text": transcript}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": to_gemini_schema(extraction_schema(tax)),
                "temperature": 0,
            },
        }
        url = self.ENDPOINT.format(model=self.model) + f"?key={self.api_key}"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=120, context=_ssl_context()) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Gemini {exc.code}: {exc.read().decode()[:400]}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        text = payload["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        usage = payload.get("usageMetadata", {})
        return _to_result(
            parsed,
            tax,
            model=self.model,
            latency_ms=latency_ms,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
        )


class StubExtractor:
    """Deterministic keyword matcher. Lets the pipeline run with no network and
    no key -- used by tests and for offline demos."""

    model = "stub-v1"
    RULES = [
        (("emi", "kist"), "objection.PRICE.EMI"),
        (("exchange", "purani", "old vehicle"), "objection.PRICE.exchange_value"),
        (("down payment", "downpayment"), "objection.PRICE.down_payment"),
        (("range", "kitna chalegi"), "objection.BATTERY.range"),
        (("charg",), "objection.BATTERY.charging"),
        (("service",), "objection.SERVICE.network"),
        (("mehenga", "expensive", "costly", "zyada"), "objection.PRICE.upfront_price"),
    ]

    def extract(self, transcript: str, tax: Taxonomy) -> ExtractionResult:
        low = transcript.lower()
        leaves = set(tax.leaf_paths())
        events = []
        for needles, node in self.RULES:
            if node not in leaves:
                continue
            hit = next((n for n in needles if n in low), None)
            if hit:
                idx = low.find(hit)
                events.append(
                    ExtractedEvent(
                        node=node,
                        slots={k: None for k in tax.slots},
                        polarity=-1,
                        intensity=None,
                        span=transcript[max(0, idx - 20) : idx + 40],
                        confidence=70,
                    )
                )
        return ExtractionResult(
            events=events,
            interaction_count=1,
            unclear=not events,
            model=self.model,
            prompt_version=PROMPT_VERSION,
        )


def _to_result(parsed: dict, tax: Taxonomy, **meta) -> ExtractionResult:
    leaves = set(tax.leaf_paths())
    events = []
    for raw in parsed.get("events", []):
        node = raw.get("node")
        # The schema should make this impossible; drop rather than trust it.
        if node not in leaves:
            continue
        events.append(
            ExtractedEvent(
                node=node,
                slots={name: raw.get(name) for name in tax.slots},
                polarity=int(raw.get("polarity", 0)),
                intensity=raw.get("intensity"),
                span=raw.get("span", ""),
                confidence=int(raw.get("confidence", 0)),
            )
        )
    return ExtractionResult(
        events=events,
        interaction_count=int(parsed.get("interaction_count", 1)),
        unclear=bool(parsed.get("unclear", False)),
        prompt_version=PROMPT_VERSION,
        **meta,
    )


def get_extractor(provider: str | None = None) -> Extractor:
    name = (provider or settings.llm_provider).lower()
    if name == "gemini":
        return GeminiExtractor()
    if name == "stub":
        return StubExtractor()
    raise ValueError(f"unknown extraction provider: {name}")
