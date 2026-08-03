"""Deterministic OpenRouter reference projection tests."""

from __future__ import annotations

import copy
import json

import pytest

import scripts.openrouter_references as reference_tool
from scripts.openrouter_references import (
    FetchedSource,
    SourceSpec,
    build_snapshot,
    drift_view,
    snapshot_diff,
    validate_manifest,
)


def _manifest() -> dict[str, object]:
    def source(source_id: str, projection: str) -> dict[str, str]:
        extension = "txt" if source_id == "docs-index" else "json"
        return {
            "id": source_id,
            "url": f"https://openrouter.ai/{source_id}",
            "format": "text" if source_id == "docs-index" else "json",
            "destination": f"{source_id}.{extension}",
            "projection": projection,
        }

    return {
        "schema_version": 1,
        "provider": "openrouter",
        "reviewed_snapshot": "docs/inference/test-reviewed.json",
        "references_root": "references/openrouter-test",
        "default_policy_profile": "private_zdr",
        "policy_profiles": {
            "private_zdr": {
                "allow_fallbacks": True,
                "allowed_contexts": ["group", "inline", "private"],
                "data_collection": "deny",
                "require_parameters": True,
                "requires_versioned_consent": False,
                "zero_data_retention": True,
            },
            "consented_non_zdr_free": {
                "allow_fallbacks": True,
                "allowed_contexts": ["inline", "private"],
                "data_collection": "allow",
                "require_parameters": True,
                "requires_versioned_consent": True,
                "zero_data_retention": False,
            },
        },
        "openapi_paths": [
            "/audio/transcriptions",
            "/chat/completions",
            "/images",
            "/videos",
        ],
        "sources": [
            source("docs-index", "docs_index"),
            source("openapi", "openapi_contract"),
            source("models", "primary_models"),
            source("image-models", "image_models"),
            source("video-models", "video_models"),
            source("zdr-endpoints", "zdr_endpoints"),
        ],
        "endpoint_sources": [
            {
                "id": "model-endpoints",
                "catalog": "primary",
                "url_template": "https://openrouter.ai/api/v1/models/{slug}/endpoints",
                "destination": "model-endpoints/{slug_file}.json",
                "projection": "model_endpoints",
            },
            {
                "id": "image-endpoints",
                "catalog": "images",
                "url_template": "https://openrouter.ai/api/v1/images/models/{slug}/endpoints",
                "destination": "image-endpoints/{slug_file}.json",
                "projection": "image_endpoints",
            },
        ],
        "models": [
            {
                "key": "text",
                "name": "Text",
                "slug": "vendor/text",
                "surface": "primary",
                "api_path": "/chat/completions",
                "policy_profile": "private_zdr",
            },
            {
                "key": "image",
                "name": "Image",
                "slug": "vendor/image",
                "surface": "images",
                "api_path": "/images",
                "policy_profile": "private_zdr",
            },
            {
                "key": "video",
                "name": "Video",
                "slug": "vendor/video",
                "surface": "videos",
                "api_path": "/videos",
                "policy_profile": "private_zdr",
            },
            {
                "key": "stt",
                "name": "STT",
                "slug": "vendor/stt",
                "surface": "transcriptions",
                "api_path": "/audio/transcriptions",
                "policy_profile": "private_zdr",
            },
            {
                "key": "tts",
                "name": "TTS",
                "slug": "vendor/tts",
                "surface": "speech",
                "api_path": "/audio/speech",
                "policy_profile": "private_zdr",
            },
            {
                "key": "free",
                "name": "Free",
                "slug": "vendor/free:free",
                "surface": "primary",
                "api_path": "/chat/completions",
                "policy_profile": "consented_non_zdr_free",
            },
        ],
    }


def _primary_record(model_id: str) -> dict[str, object]:
    return {
        "id": model_id,
        "canonical_slug": f"{model_id}-20260721",
        "name": model_id,
        "created": 1,
        "context_length": 1000,
        "architecture": {
            "input_modalities": ["text"],
            "output_modalities": ["text"],
        },
        "pricing": {"completion": "0.000002", "prompt": "0.000001"},
        "top_provider": {"max_completion_tokens": 100},
        "supported_parameters": ["tools", "temperature"],
    }


def _fetched(
    manifest: dict[str, object], *, reverse: bool = False
) -> dict[str, FetchedSource]:
    primary = [
        _primary_record("vendor/text"),
        _primary_record("vendor/image"),
        _primary_record("vendor/free:free"),
    ]
    if reverse:
        primary.reverse()
    values: dict[str, object] = {
        "docs-index": "# OpenRouter\n",
        "openapi": {
            "openapi": "3.1.0",
            "info": {"version": "1"},
            "servers": [{"url": "https://openrouter.ai/api/v1"}],
            "paths": {
                "/audio/transcriptions": {"post": {"example": {"model": "vendor/stt"}}},
                "/chat/completions": {"post": {}},
                "/images": {"post": {}},
                "/videos": {"post": {}},
            },
            "components": {},
        },
        "models": {"data": primary},
        "image-models": {
            "data": [
                {
                    "id": "vendor/image",
                    "name": "Image",
                    "architecture": {
                        "input_modalities": ["text"],
                        "output_modalities": ["image"],
                    },
                    "supported_parameters": {"resolution": ["2K", "1K"]},
                }
            ]
        },
        "video-models": {
            "data": [
                {
                    "id": "vendor/video",
                    "canonical_slug": "vendor/video-20260721",
                    "name": "Video",
                    "supported_durations": [8, 4],
                    "pricing_skus": {"second": "0.10"},
                }
            ]
        },
        "zdr-endpoints": {
            "data": [
                {
                    "model_id": "vendor/text",
                    "provider_name": "Provider",
                    "tag": "provider/zdr",
                    "pricing": {"prompt": "0.000001"},
                    "status": 0,
                    "uptime_last_5m": 99.9,
                },
                {
                    "model_id": "vendor/image",
                    "provider_name": "Provider",
                    "tag": "provider/zdr",
                    "pricing": {"prompt": "0.000001"},
                },
            ]
        },
    }
    fetched: dict[str, FetchedSource] = {}
    sources = manifest["sources"]
    assert isinstance(sources, list)
    for source_value in sources:
        assert isinstance(source_value, dict)
        source_id = source_value["id"]
        assert isinstance(source_id, str)
        value = values[source_id]
        raw = (
            value.encode()
            if isinstance(value, str)
            else json.dumps(value, separators=(",", ":")).encode()
        )
        fetched[source_id] = FetchedSource(
            SourceSpec(
                id=source_id,
                url=str(source_value["url"]),
                format=str(source_value["format"]),
                destination=str(source_value["destination"]),
                projection=str(source_value["projection"]),
            ),
            raw,
            value,
        )
    image_endpoint_value = {
        "id": "vendor/image",
        "endpoints": [
            {
                "provider_name": "Provider",
                "provider_slug": "provider/zdr",
                "provider_tag": "provider/zdr",
                "supported_parameters": {},
                "allowed_passthrough_parameters": [],
                "supports_streaming": False,
                "pricing": [],
            }
        ],
    }
    fetched["image-endpoints:vendor/image"] = FetchedSource(
        SourceSpec(
            id="image-endpoints:vendor/image",
            url="https://openrouter.ai/api/v1/images/models/vendor/image/endpoints",
            format="json",
            destination="image-endpoints/vendor-image.json",
            projection="image_endpoints",
        ),
        json.dumps(image_endpoint_value).encode(),
        image_endpoint_value,
    )
    return fetched


def test_snapshot_marks_catalog_surfaces_and_fail_closed_models() -> None:
    manifest = _manifest()
    snapshot = build_snapshot(
        manifest,
        _fetched(manifest),
        retrieved_at="2026-07-21T12:00:00Z",
    )
    models = {model["key"]: model for model in snapshot["models"]}

    assert models["text"]["availability"] == "primary"
    assert models["image"]["availability"] == "primary_and_dedicated"
    assert models["video"]["availability"] == "dedicated_only"
    assert models["stt"]["availability"] == "documented_only"
    assert models["tts"]["availability"] == "unavailable"
    assert models["free"]["availability"] == "primary"
    assert snapshot["primary_catalog_missing"] == [
        "vendor/stt",
        "vendor/tts",
        "vendor/video",
    ]
    assert snapshot["catalog_unavailable"] == ["vendor/stt", "vendor/tts"]
    assert snapshot["runtime_unavailable"] == [
        "vendor/stt",
        "vendor/tts",
        "vendor/video",
    ]
    assert snapshot["private_zdr_unavailable"] == [
        "vendor/free:free",
        "vendor/stt",
        "vendor/tts",
        "vendor/video",
    ]
    assert models["text"]["zdr_provider_tags"] == ["provider/zdr"]
    assert models["text"]["policy_readiness"]["runtime_ready"] is True
    assert models["image"]["policy_readiness"]["runtime_ready"] is True
    assert models["video"]["policy_readiness"]["runtime_ready"] is False
    assert models["video"]["policy_readiness"]["zero_data_retention"] == "unverified"
    assert models["free"]["policy_readiness"] == {
        "selected_profile": "consented_non_zdr_free",
        "runtime_ready": True,
        "allowed_contexts": ["inline", "private"],
        "requires_versioned_consent": True,
        "zero_data_retention": "not_required",
        "blockers": [],
        "private_zdr": {
            "runtime_ready": False,
            "zero_data_retention": "unavailable",
            "blockers": ["zero_data_retention_unavailable"],
        },
    }


def test_review_projection_is_order_and_retrieval_time_independent() -> None:
    manifest = _manifest()
    first = build_snapshot(
        manifest,
        _fetched(manifest),
        retrieved_at="2026-07-21T12:00:00Z",
    )
    second = build_snapshot(
        manifest,
        _fetched(manifest, reverse=True),
        retrieved_at="2026-07-21T13:00:00Z",
    )

    assert drift_view(first) == drift_view(second)
    assert snapshot_diff(first, second) == ""

    second["models"][0]["primary_catalog"]["pricing"] = {"prompt": "9"}
    assert snapshot_diff(first, second)


def test_manifest_rejects_non_openrouter_source() -> None:
    manifest = copy.deepcopy(_manifest())
    manifest["sources"][0]["url"] = "https://example.com/llms.txt"

    with pytest.raises(reference_tool.ReferenceError, match="OpenRouter HTTPS origin"):
        validate_manifest(manifest)
