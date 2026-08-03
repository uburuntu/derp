#!/usr/bin/env python3
"""Refresh and verify reviewed OpenRouter catalog evidence."""

from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import json
import os
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPOSITORY_ROOT / "docs/inference/openrouter-sources.json"
OPENROUTER_HOST = "openrouter.ai"
USER_AGENT = "derp-openrouter-reference/1"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
FETCH_WORKERS = 4
FETCH_TIMEOUT_SECONDS = 30

_SOURCE_ID = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_MODEL_KEY = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_MODEL_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._:-]*$")
_FORMATS = frozenset({"json", "text"})
_PROJECTIONS = frozenset(
    {
        "docs_index",
        "full",
        "image_endpoints",
        "image_models",
        "model_endpoints",
        "openapi_contract",
        "primary_models",
        "video_models",
        "zdr_endpoints",
    }
)
_SURFACES = frozenset({"images", "primary", "speech", "transcriptions", "videos"})

type JsonObject = dict[str, Any]


class ReferenceError(RuntimeError):  # noqa: A001 - domain-specific reference failure
    """The manifest, upstream evidence, or reviewed snapshot is invalid."""


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """One concrete source fetched into the ignored reference workspace."""

    id: str
    url: str
    format: str
    destination: str
    projection: str


@dataclass(frozen=True, slots=True)
class FetchedSource:
    """One exact response plus its decoded value."""

    spec: SourceSpec
    raw: bytes
    value: Any


class _OpenRouterRedirectHandler(HTTPRedirectHandler):
    """Allow redirects only within OpenRouter's HTTPS origin."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        redirected = urljoin(req.full_url, newurl)
        _validate_url(redirected)
        return super().redirect_request(req, fp, code, msg, headers, redirected)


def load_manifest(path: Path = DEFAULT_MANIFEST) -> JsonObject:
    """Load and validate the tracked source manifest."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReferenceError(f"cannot load OpenRouter manifest: {path}") from exc
    if not isinstance(value, dict):
        raise ReferenceError("OpenRouter manifest must be a JSON object")
    validate_manifest(value)
    return value


def validate_manifest(manifest: JsonObject) -> None:
    """Reject ambiguous, unsafe, or incomplete manifest state."""
    if manifest.get("schema_version") != 1:
        raise ReferenceError("unsupported OpenRouter manifest schema_version")
    if manifest.get("provider") != "openrouter":
        raise ReferenceError("OpenRouter manifest provider must be openrouter")

    _validate_relative_path(_required_string(manifest, "reviewed_snapshot"))
    _validate_relative_path(_required_string(manifest, "references_root"))

    default_policy_profile = _required_string(manifest, "default_policy_profile")
    policy_profiles = _required_dict(manifest, "policy_profiles")
    expected_policy_keys = {
        "allow_fallbacks",
        "allowed_contexts",
        "data_collection",
        "require_parameters",
        "requires_versioned_consent",
        "zero_data_retention",
    }
    if default_policy_profile != "private_zdr":
        raise ReferenceError("private_zdr must remain the default policy profile")
    for profile_name, profile_value in policy_profiles.items():
        if not isinstance(profile_name, str) or not _MODEL_KEY.fullmatch(profile_name):
            raise ReferenceError("policy profile names must be snake_case")
        profile = _required_mapping(profile_value, "policy profile")
        if set(profile) != expected_policy_keys:
            raise ReferenceError("policy profile has unexpected or missing fields")
        if profile["data_collection"] not in {"allow", "deny"} or any(
            not isinstance(profile[name], bool)
            for name in (
                "allow_fallbacks",
                "require_parameters",
                "requires_versioned_consent",
                "zero_data_retention",
            )
        ):
            raise ReferenceError("policy profile has invalid explicit values")
        contexts = profile["allowed_contexts"]
        if (
            not isinstance(contexts, list)
            or not contexts
            or contexts != sorted(set(contexts))
            or set(contexts) - {"group", "inline", "private"}
        ):
            raise ReferenceError("policy profile allowed_contexts are invalid")
    strict_policy = _required_mapping(policy_profiles.get("private_zdr"), "private_zdr")
    if (
        strict_policy["data_collection"] != "deny"
        or strict_policy["zero_data_retention"] is not True
        or strict_policy["requires_versioned_consent"] is not False
    ):
        raise ReferenceError("private_zdr must remain fail closed")
    consented_free = _required_mapping(
        policy_profiles.get("consented_non_zdr_free"),
        "consented_non_zdr_free",
    )
    if (
        consented_free["allowed_contexts"] != ["inline", "private"]
        or consented_free["data_collection"] != "allow"
        or consented_free["zero_data_retention"] is not False
        or consented_free["requires_versioned_consent"] is not True
    ):
        raise ReferenceError(
            "consented_non_zdr_free requires private/inline versioned consent"
        )

    openapi_paths = _required_list(manifest, "openapi_paths")
    if not openapi_paths or any(
        not isinstance(path, str) or not path.startswith("/") for path in openapi_paths
    ):
        raise ReferenceError("openapi_paths must contain absolute API paths")
    if len(openapi_paths) != len(set(openapi_paths)):
        raise ReferenceError("openapi_paths must be unique")

    source_ids: set[str] = set()
    destinations: set[str] = set()
    for source in _required_list(manifest, "sources"):
        spec = _parse_source(source)
        if spec.id in source_ids or spec.destination in destinations:
            raise ReferenceError("source IDs and destinations must be unique")
        source_ids.add(spec.id)
        destinations.add(spec.destination)
    required_sources = {
        "docs-index",
        "image-models",
        "models",
        "openapi",
        "video-models",
        "zdr-endpoints",
    }
    if not required_sources <= source_ids:
        raise ReferenceError("manifest is missing a required OpenRouter source")

    endpoint_ids: set[str] = set()
    for item in _required_list(manifest, "endpoint_sources"):
        endpoint = _required_mapping(item, "endpoint source")
        endpoint_id = _required_string(endpoint, "id")
        if not _SOURCE_ID.fullmatch(endpoint_id) or endpoint_id in endpoint_ids:
            raise ReferenceError("endpoint source IDs must be unique safe names")
        endpoint_ids.add(endpoint_id)
        if endpoint.get("catalog") not in {"primary", "images"}:
            raise ReferenceError("endpoint source catalog must be primary or images")
        url_template = _required_string(endpoint, "url_template")
        if url_template.count("{slug}") != 1:
            raise ReferenceError("endpoint URL templates require one {slug}")
        _validate_url(url_template.replace("{slug}", "example/model"))
        destination = _required_string(endpoint, "destination")
        if destination.count("{slug_file}") != 1:
            raise ReferenceError(
                "endpoint destinations require one {slug_file} placeholder"
            )
        _validate_relative_path(destination.replace("{slug_file}", "model.json"))
        projection = _required_string(endpoint, "projection")
        if projection not in {"model_endpoints", "image_endpoints"}:
            raise ReferenceError("endpoint source projection is unsupported")

    model_keys: set[str] = set()
    model_slugs: set[str] = set()
    models = _required_list(manifest, "models")
    if not models:
        raise ReferenceError("manifest must select at least one model")
    for item in models:
        model = _required_mapping(item, "model")
        key = _required_string(model, "key")
        slug = _required_string(model, "slug")
        _required_string(model, "name")
        api_path = _required_string(model, "api_path")
        policy_profile = _required_string(model, "policy_profile")
        if not _MODEL_KEY.fullmatch(key) or key in model_keys:
            raise ReferenceError("model keys must be unique snake_case names")
        if not _MODEL_SLUG.fullmatch(slug) or slug in model_slugs:
            raise ReferenceError("model slugs must be unique OpenRouter slugs")
        if model.get("surface") not in _SURFACES:
            raise ReferenceError("model surface is unsupported")
        if not api_path.startswith("/"):
            raise ReferenceError("model api_path must be absolute")
        if policy_profile not in policy_profiles:
            raise ReferenceError("model policy_profile is not defined")
        if policy_profile == "consented_non_zdr_free" and not slug.endswith(":free"):
            raise ReferenceError("non-ZDR free policy requires a :free model slug")
        model_keys.add(key)
        model_slugs.add(slug)


def refresh(manifest: JsonObject, *, retrieved_at: str | None = None) -> JsonObject:
    """Fetch all sources and write raw evidence, candidate, and review diff."""
    timestamp = retrieved_at or _utc_timestamp()
    static_specs = tuple(_parse_source(item) for item in manifest["sources"])
    fetched = _fetch_many(static_specs)
    endpoint_specs = _expand_endpoint_sources(manifest, fetched)
    fetched.update(_fetch_many(endpoint_specs))

    paths = _workspace_paths(manifest)
    for source in fetched.values():
        _atomic_write(paths["raw"] / source.spec.destination, source.raw)

    candidate = build_snapshot(manifest, fetched, retrieved_at=timestamp)
    metadata = {
        "schema_version": 1,
        "retrieved_at": timestamp,
        "sources": [
            _source_evidence(manifest, source, timestamp)
            for source in sorted(fetched.values(), key=lambda item: item.spec.id)
        ],
    }
    _write_json(paths["metadata"], metadata)
    _write_json(paths["candidate"], candidate)
    baseline = _load_optional_json(paths["baseline"])
    _atomic_write(paths["diff"], snapshot_diff(baseline, candidate).encode())
    _print_summary(candidate, paths)
    return candidate


def build_snapshot(
    manifest: JsonObject,
    fetched: dict[str, FetchedSource],
    *,
    retrieved_at: str,
) -> JsonObject:
    """Build one deterministic reviewed projection from decoded sources."""
    validate_manifest(manifest)
    primary = _records_by_id(_source_value(fetched, "models"))
    images = _records_by_id(_source_value(fetched, "image-models"))
    videos = _records_by_id(_source_value(fetched, "video-models"))
    openapi = _required_object_value(_source_value(fetched, "openapi"), "openapi")
    zdr = _zdr_by_model(_source_value(fetched, "zdr-endpoints"))

    models: list[JsonObject] = []
    primary_missing: list[str] = []
    catalog_unavailable: list[str] = []
    private_zdr_unavailable: list[str] = []
    runtime_unavailable: list[str] = []
    for selected_value in manifest["models"]:
        selected = _required_mapping(selected_value, "model")
        slug = selected["slug"]
        primary_record = primary.get(slug)
        image_record = images.get(slug)
        video_record = videos.get(slug)
        dedicated_record = image_record or video_record
        documented = _contains_exact_string(openapi, slug)
        api_path = selected["api_path"]
        api_contract_available = api_path in _required_dict(openapi, "paths")

        if primary_record is None:
            primary_missing.append(slug)
        availability = _availability(
            primary=primary_record is not None,
            dedicated=dedicated_record is not None,
            documented=documented,
        )
        if availability in {"documented_only", "unavailable"}:
            catalog_unavailable.append(slug)

        primary_endpoints = _endpoint_projection_for(
            fetched,
            "model-endpoints",
            slug,
            _normalize_model_endpoints,
        )
        image_endpoints = _endpoint_projection_for(
            fetched,
            "image-endpoints",
            slug,
            _normalize_image_endpoints,
        )
        zdr_provider_tags = sorted(
            {endpoint["tag"] for endpoint in zdr.get(slug, ()) if endpoint.get("tag")}
        )
        policy_readiness = _policy_readiness(
            manifest["policy_profiles"],
            selected_profile=selected["policy_profile"],
            surface=selected["surface"],
            availability=availability,
            zdr_provider_tags=zdr_provider_tags,
            dedicated_endpoints=image_endpoints,
        )
        if not policy_readiness["runtime_ready"]:
            runtime_unavailable.append(slug)
        if not policy_readiness["private_zdr"]["runtime_ready"]:
            private_zdr_unavailable.append(slug)
        models.append(
            {
                "key": selected["key"],
                "name": selected["name"],
                "approved_slug": slug,
                "surface": selected["surface"],
                "api_path": api_path,
                "policy_profile": selected["policy_profile"],
                "availability": availability,
                "primary_catalog_available": primary_record is not None,
                "dedicated_catalog_available": dedicated_record is not None,
                "openapi_documented": documented,
                "api_contract_available": api_contract_available,
                "canonical_slug": _canonical_slug(primary_record, dedicated_record),
                "primary_catalog": (
                    _normalize_primary_model(primary_record)
                    if primary_record is not None
                    else None
                ),
                "dedicated_catalog": (
                    _normalize_image_model(image_record)
                    if image_record is not None
                    else _normalize_video_model(video_record)
                    if video_record is not None
                    else None
                ),
                "provider_endpoints": primary_endpoints,
                "dedicated_endpoints": image_endpoints,
                "zdr_provider_tags": zdr_provider_tags,
                "policy_readiness": policy_readiness,
            }
        )

    evidence = [
        _source_evidence(manifest, source, retrieved_at)
        for source in sorted(fetched.values(), key=lambda item: item.spec.id)
    ]
    return {
        "schema_version": 1,
        "provider": "openrouter",
        "retrieved_at": retrieved_at,
        "manifest_sha256": _sha256(_canonical_json(manifest)),
        "default_policy_profile": manifest["default_policy_profile"],
        "policy_profiles": copy.deepcopy(manifest["policy_profiles"]),
        "sources": evidence,
        "primary_catalog_missing": sorted(primary_missing),
        "catalog_unavailable": sorted(catalog_unavailable),
        "private_zdr_unavailable": sorted(private_zdr_unavailable),
        "runtime_unavailable": sorted(runtime_unavailable),
        "models": sorted(models, key=lambda item: item["key"]),
    }


def snapshot_diff(baseline: Any | None, candidate: JsonObject) -> str:
    """Return a deterministic unified diff without retrieval-only metadata."""
    before = (
        _canonical_json(drift_view(baseline or {})).decode().splitlines(keepends=True)
    )
    after = _canonical_json(drift_view(candidate)).decode().splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            before,
            after,
            fromfile="reviewed/openrouter-reviewed.json",
            tofile="candidate/openrouter-reviewed.json",
        )
    )


def drift_view(snapshot: Any) -> Any:
    """Remove retrieval evidence that must not trigger semantic drift."""
    value = copy.deepcopy(snapshot)
    if not isinstance(value, dict):
        return value
    value.pop("retrieved_at", None)
    sources = value.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, dict):
                source.pop("retrieved_at", None)
                source.pop("response_bytes", None)
                source.pop("response_sha256", None)
    return value


def check(manifest: JsonObject, *, offline: bool) -> bool:
    """Refresh unless offline and report whether candidate matches baseline."""
    paths = _workspace_paths(manifest)
    if offline:
        candidate = _load_required_json(paths["candidate"], "candidate")
    else:
        candidate = refresh(manifest)
    baseline = _load_optional_json(paths["baseline"])
    if baseline is None:
        print(f"No reviewed OpenRouter baseline: {paths['baseline']}", file=sys.stderr)
        print(f"Review {paths['candidate']} and run accept --yes.", file=sys.stderr)
        return False
    _validate_snapshot(candidate, manifest)
    _validate_snapshot(baseline, manifest)
    diff = snapshot_diff(baseline, candidate)
    _atomic_write(paths["diff"], diff.encode())
    if diff:
        print("OpenRouter reference drift detected.", file=sys.stderr)
        print(f"Candidate: {paths['candidate']}", file=sys.stderr)
        print(f"Review diff: {paths['diff']}", file=sys.stderr)
        return False
    print("OpenRouter references match the reviewed baseline.")
    _print_availability(candidate)
    return True


def accept(manifest: JsonObject, *, confirmed: bool) -> None:
    """Promote an already-reviewed candidate without another network fetch."""
    if not confirmed:
        raise ReferenceError(
            "accept requires --yes after reviewing raw sources and diff"
        )
    paths = _workspace_paths(manifest)
    candidate = _load_required_json(paths["candidate"], "candidate")
    _validate_snapshot(candidate, manifest)
    _write_json(paths["baseline"], candidate)
    _atomic_write(paths["diff"], b"")
    print(f"Accepted reviewed OpenRouter snapshot: {paths['baseline']}")
    _print_availability(candidate)


def _fetch_many(specs: tuple[SourceSpec, ...]) -> dict[str, FetchedSource]:
    if not specs:
        return {}
    fetched: dict[str, FetchedSource] = {}
    with ThreadPoolExecutor(max_workers=min(FETCH_WORKERS, len(specs))) as pool:
        futures = {pool.submit(_fetch_source, spec): spec for spec in specs}
        for future in as_completed(futures):
            spec = futures[future]
            try:
                fetched[spec.id] = future.result()
            except Exception as exc:
                raise ReferenceError(
                    f"failed to fetch OpenRouter source {spec.id}"
                ) from exc
    return fetched


def _fetch_source(spec: SourceSpec) -> FetchedSource:
    _validate_url(spec.url)
    request = Request(  # noqa: S310 - URL is validated to one HTTPS origin
        spec.url,
        headers={
            "Accept": "application/json" if spec.format == "json" else "text/plain",
            "User-Agent": USER_AGENT,
        },
    )
    opener = build_opener(_OpenRouterRedirectHandler())
    try:
        with opener.open(  # noqa: S310 - URL is validated to one HTTPS origin
            request,
            timeout=FETCH_TIMEOUT_SECONDS,
        ) as response:
            _validate_url(response.geturl())
            content_type = response.headers.get_content_type().lower()
            expected = "application/json" if spec.format == "json" else "text/"
            if expected not in content_type:
                raise ReferenceError(
                    f"source {spec.id} returned unexpected content type {content_type}"
                )
            declared_length = response.headers.get("Content-Length")
            if declared_length and int(declared_length) > MAX_RESPONSE_BYTES:
                raise ReferenceError(f"source {spec.id} exceeds the response limit")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise ReferenceError(f"source {spec.id} request failed") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ReferenceError(f"source {spec.id} exceeds the response limit")
    if not raw:
        raise ReferenceError(f"source {spec.id} returned an empty response")
    value = _decode_source(spec, raw)
    return FetchedSource(spec=spec, raw=raw, value=value)


def _decode_source(spec: SourceSpec, raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReferenceError(f"source {spec.id} is not UTF-8") from exc
    if spec.format == "text":
        return _normalize_text(text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReferenceError(f"source {spec.id} returned invalid JSON") from exc
    if not isinstance(value, dict | list):
        raise ReferenceError(f"source {spec.id} JSON must be an object or array")
    return value


def _expand_endpoint_sources(
    manifest: JsonObject,
    fetched: dict[str, FetchedSource],
) -> tuple[SourceSpec, ...]:
    primary = _records_by_id(_source_value(fetched, "models"))
    images = _records_by_id(_source_value(fetched, "image-models"))
    specs: list[SourceSpec] = []
    for endpoint_value in manifest["endpoint_sources"]:
        endpoint = _required_mapping(endpoint_value, "endpoint source")
        catalog = primary if endpoint["catalog"] == "primary" else images
        for model_value in manifest["models"]:
            model = _required_mapping(model_value, "model")
            slug = model["slug"]
            if slug not in catalog:
                continue
            specs.append(
                SourceSpec(
                    id=f"{endpoint['id']}:{slug}",
                    url=endpoint["url_template"].replace(
                        "{slug}", quote(slug, safe="/:")
                    ),
                    format="json",
                    destination=endpoint["destination"].replace(
                        "{slug_file}", quote(slug, safe="")
                    ),
                    projection=endpoint["projection"],
                )
            )
    return tuple(sorted(specs, key=lambda item: item.id))


def _source_evidence(
    manifest: JsonObject,
    source: FetchedSource,
    retrieved_at: str,
) -> JsonObject:
    projection = _source_projection(source, manifest)
    return {
        "id": source.spec.id,
        "url": source.spec.url,
        "retrieved_at": retrieved_at,
        "response_bytes": len(source.raw),
        "response_sha256": _sha256(source.raw),
        "review_sha256": _sha256(_projection_bytes(projection)),
    }


def _source_projection(source: FetchedSource, manifest: JsonObject) -> Any:
    projection = source.spec.projection
    selected = [_required_mapping(item, "model") for item in manifest["models"]]
    slugs = [item["slug"] for item in selected]
    if projection == "full":
        return source.value
    if projection == "docs_index":
        text = _required_text_value(source.value, source.spec.id)
        expected_urls = sorted(
            item["url"]
            for item in manifest["sources"]
            if item["format"] == "text" and item["id"] != "docs-index"
        )
        lines = text.splitlines()
        return [
            {
                "url": url,
                "entry": next((line.strip() for line in lines if url in line), None),
            }
            for url in expected_urls
        ]
    if projection == "openapi_contract":
        return _openapi_projection(
            _required_object_value(source.value, source.spec.id),
            manifest["openapi_paths"],
        )
    if projection == "primary_models":
        return _selected_catalog_projection(
            source.value,
            slugs,
            _normalize_primary_model,
        )
    if projection == "image_models":
        image_slugs = [item["slug"] for item in selected if item["surface"] == "images"]
        return _selected_catalog_projection(
            source.value,
            image_slugs,
            _normalize_image_model,
        )
    if projection == "video_models":
        video_slugs = [item["slug"] for item in selected if item["surface"] == "videos"]
        return _selected_catalog_projection(
            source.value,
            video_slugs,
            _normalize_video_model,
        )
    if projection == "zdr_endpoints":
        grouped = _zdr_by_model(source.value)
        return {slug: grouped.get(slug, []) for slug in sorted(slugs)}
    if projection == "model_endpoints":
        return _normalize_model_endpoints(source.value)
    if projection == "image_endpoints":
        return _normalize_image_endpoints(source.value)
    raise ReferenceError(f"unsupported source projection: {projection}")


def _selected_catalog_projection(
    value: Any,
    slugs: list[str],
    normalizer: Any,
) -> JsonObject:
    records = _records_by_id(value)
    return {
        slug: normalizer(records[slug]) if slug in records else None
        for slug in sorted(slugs)
    }


def _normalize_primary_model(record: JsonObject) -> JsonObject:
    return {
        key: _stable_value(record.get(key))
        for key in (
            "id",
            "canonical_slug",
            "name",
            "created",
            "context_length",
            "architecture",
            "pricing",
            "top_provider",
            "per_request_limits",
            "supported_parameters",
            "supported_voices",
            "knowledge_cutoff",
            "expiration_date",
            "reasoning",
        )
    }


def _normalize_image_model(record: JsonObject) -> JsonObject:
    return {
        key: _stable_value(record.get(key))
        for key in (
            "id",
            "canonical_slug",
            "name",
            "created",
            "architecture",
            "supported_parameters",
            "supports_streaming",
        )
    }


def _normalize_video_model(record: JsonObject) -> JsonObject:
    return {
        key: _stable_value(record.get(key))
        for key in (
            "id",
            "canonical_slug",
            "name",
            "created",
            "supported_resolutions",
            "supported_aspect_ratios",
            "supported_sizes",
            "supported_durations",
            "supported_frame_images",
            "generate_audio",
            "seed",
            "pricing_skus",
            "allowed_passthrough_parameters",
        )
    }


def _normalize_model_endpoints(value: Any) -> JsonObject:
    payload = _required_object_value(value, "model endpoints")
    data = _required_dict(payload, "data")
    endpoints = _required_list(data, "endpoints")
    return {
        "id": data.get("id"),
        "architecture": _stable_value(data.get("architecture")),
        "endpoints": sorted(
            (
                _normalize_model_endpoint(_required_mapping(item, "model endpoint"))
                for item in endpoints
            ),
            key=lambda item: (item["tag"] or "", item["provider_name"] or ""),
        ),
    }


def _normalize_model_endpoint(endpoint: JsonObject) -> JsonObject:
    return {
        key: _stable_value(endpoint.get(key))
        for key in (
            "provider_name",
            "tag",
            "quantization",
            "context_length",
            "max_prompt_tokens",
            "max_completion_tokens",
            "pricing",
            "supported_parameters",
            "supports_implicit_caching",
        )
    }


def _normalize_image_endpoints(value: Any) -> JsonObject:
    payload = _required_object_value(value, "image endpoints")
    endpoints = _required_list(payload, "endpoints")
    normalized = [
        {
            key: _stable_value(endpoint.get(key))
            for key in (
                "provider_name",
                "provider_slug",
                "provider_tag",
                "supported_parameters",
                "allowed_passthrough_parameters",
                "supports_streaming",
                "pricing",
            )
        }
        for item in endpoints
        if (endpoint := _required_mapping(item, "image endpoint"))
    ]
    return {
        "id": payload.get("id"),
        "endpoints": sorted(
            normalized,
            key=lambda item: (
                item["provider_tag"] or "",
                item["provider_name"] or "",
            ),
        ),
    }


def _zdr_by_model(value: Any) -> dict[str, list[JsonObject]]:
    payload = _required_object_value(value, "ZDR endpoints")
    grouped: dict[str, list[JsonObject]] = {}
    for item in _required_list(payload, "data"):
        endpoint = _required_mapping(item, "ZDR endpoint")
        model_id = endpoint.get("model_id")
        if not isinstance(model_id, str):
            raise ReferenceError("ZDR endpoint omitted model_id")
        grouped.setdefault(model_id, []).append(_normalize_model_endpoint(endpoint))
    for endpoints in grouped.values():
        endpoints.sort(
            key=lambda item: (item["tag"] or "", item["provider_name"] or "")
        )
    return grouped


def _openapi_projection(document: JsonObject, selected_paths: list[str]) -> JsonObject:
    paths = _required_dict(document, "paths")
    projection: JsonObject = {
        "openapi": document.get("openapi"),
        "info": {"version": _required_dict(document, "info").get("version")},
        "servers": copy.deepcopy(document.get("servers")),
        "security": copy.deepcopy(document.get("security")),
        "paths": {path: copy.deepcopy(paths.get(path)) for path in selected_paths},
        "components": {},
    }
    pending = _component_refs(projection)
    seen: set[str] = set()
    while pending:
        reference = pending.pop()
        if reference in seen:
            continue
        seen.add(reference)
        parts = reference.removeprefix("#/components/").split("/", 1)
        if len(parts) != 2:
            continue
        section, encoded_name = parts
        name = encoded_name.replace("~1", "/").replace("~0", "~")
        components = _required_dict(document, "components")
        section_values = components.get(section)
        component = (
            section_values.get(name) if isinstance(section_values, dict) else None
        )
        projection_components = _required_mapping(
            projection["components"],
            "projected OpenAPI components",
        )
        projected_section = projection_components.setdefault(section, {})
        if not isinstance(projected_section, dict):
            raise ReferenceError("projected OpenAPI component section is invalid")
        projected_section[name] = copy.deepcopy(component)
        pending.update(_component_refs(component))
    return projection


def _component_refs(value: Any) -> set[str]:
    references: set[str] = set()
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/components/"):
            references.add(reference)
        for child in value.values():
            references.update(_component_refs(child))
    elif isinstance(value, list):
        for child in value:
            references.update(_component_refs(child))
    return references


def _stable_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _stable_value(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        normalized = [_stable_value(item) for item in value]
        return sorted(normalized, key=_compact_json)
    if isinstance(value, float):
        return format(Decimal(str(value)), "f")
    return value


def _records_by_id(value: Any) -> dict[str, JsonObject]:
    payload = _required_object_value(value, "catalog")
    records: dict[str, JsonObject] = {}
    for item in _required_list(payload, "data"):
        record = _required_mapping(item, "catalog record")
        model_id = record.get("id")
        if not isinstance(model_id, str) or not model_id:
            raise ReferenceError("catalog record omitted id")
        if model_id in records:
            raise ReferenceError(f"catalog contains duplicate model ID: {model_id}")
        records[model_id] = record
    return records


def _endpoint_projection_for(
    fetched: dict[str, FetchedSource],
    prefix: str,
    slug: str,
    normalizer: Any,
) -> JsonObject | None:
    source = fetched.get(f"{prefix}:{slug}")
    return normalizer(source.value) if source is not None else None


def _availability(*, primary: bool, dedicated: bool, documented: bool) -> str:
    if primary and dedicated:
        return "primary_and_dedicated"
    if primary:
        return "primary"
    if dedicated:
        return "dedicated_only"
    if documented:
        return "documented_only"
    return "unavailable"


def _policy_readiness(
    profiles: JsonObject,
    *,
    selected_profile: str,
    surface: str,
    availability: str,
    zdr_provider_tags: list[str],
    dedicated_endpoints: JsonObject | None,
) -> JsonObject:
    selected = _evaluate_policy(
        _required_mapping(profiles.get(selected_profile), selected_profile),
        surface=surface,
        availability=availability,
        zdr_provider_tags=zdr_provider_tags,
        dedicated_endpoints=dedicated_endpoints,
    )
    strict = _evaluate_policy(
        _required_mapping(profiles.get("private_zdr"), "private_zdr"),
        surface=surface,
        availability=availability,
        zdr_provider_tags=zdr_provider_tags,
        dedicated_endpoints=dedicated_endpoints,
    )
    profile = _required_mapping(profiles.get(selected_profile), selected_profile)
    return {
        "selected_profile": selected_profile,
        "runtime_ready": selected["runtime_ready"],
        "allowed_contexts": copy.deepcopy(profile["allowed_contexts"]),
        "requires_versioned_consent": profile["requires_versioned_consent"],
        "zero_data_retention": selected["zero_data_retention"],
        "blockers": selected["blockers"],
        "private_zdr": strict,
    }


def _evaluate_policy(
    policy: JsonObject,
    *,
    surface: str,
    availability: str,
    zdr_provider_tags: list[str],
    dedicated_endpoints: JsonObject | None,
) -> JsonObject:
    blockers: list[str] = []
    if availability in {"documented_only", "unavailable"}:
        blockers.append("catalog_unavailable")

    zdr_state = "not_required"
    if policy["zero_data_retention"]:
        if blockers:
            zdr_state = "not_evaluable"
        elif surface == "images":
            dedicated_tags = {
                endpoint.get("provider_tag")
                for endpoint in (dedicated_endpoints or {}).get("endpoints", ())
                if isinstance(endpoint, dict)
            }
            if dedicated_tags & set(zdr_provider_tags):
                zdr_state = "verified"
            else:
                zdr_state = "unverified"
                blockers.append("zero_data_retention_unverified")
        elif availability == "dedicated_only":
            zdr_state = "unverified"
            blockers.append("zero_data_retention_unverified")
        elif zdr_provider_tags:
            zdr_state = "verified"
        else:
            zdr_state = "unavailable"
            blockers.append("zero_data_retention_unavailable")

    return {
        "runtime_ready": not blockers,
        "zero_data_retention": zdr_state,
        "blockers": blockers,
    }


def _canonical_slug(
    primary: JsonObject | None,
    dedicated: JsonObject | None,
) -> str | None:
    value = (primary or {}).get("canonical_slug") or (dedicated or {}).get(
        "canonical_slug"
    )
    return value if isinstance(value, str) else None


def _contains_exact_string(value: Any, expected: str) -> bool:
    if value == expected:
        return True
    if isinstance(value, dict):
        return any(_contains_exact_string(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains_exact_string(item, expected) for item in value)
    return False


def _parse_source(value: Any) -> SourceSpec:
    source = _required_mapping(value, "source")
    source_id = _required_string(source, "id")
    source_format = _required_string(source, "format")
    destination = _required_string(source, "destination")
    projection = _required_string(source, "projection")
    url = _required_string(source, "url")
    if not _SOURCE_ID.fullmatch(source_id):
        raise ReferenceError("source id must be a safe kebab-case name")
    if source_format not in _FORMATS:
        raise ReferenceError(f"source {source_id} has unsupported format")
    if projection not in _PROJECTIONS:
        raise ReferenceError(f"source {source_id} has unsupported projection")
    _validate_relative_path(destination)
    _validate_url(url)
    return SourceSpec(source_id, url, source_format, destination, projection)


def _validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != OPENROUTER_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.fragment
    ):
        raise ReferenceError("source URLs must use the OpenRouter HTTPS origin")


def _validate_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ReferenceError("manifest paths must stay within the repository")


def _workspace_paths(manifest: JsonObject) -> dict[str, Path]:
    references = REPOSITORY_ROOT / manifest["references_root"]
    return {
        "baseline": REPOSITORY_ROOT / manifest["reviewed_snapshot"],
        "candidate": references / "openrouter-reviewed.candidate.json",
        "diff": references / "openrouter-reviewed.diff",
        "metadata": references / "fetch-metadata.json",
        "raw": references / "raw",
    }


def _validate_snapshot(snapshot: Any, manifest: JsonObject) -> None:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise ReferenceError("OpenRouter snapshot has an unsupported schema")
    if snapshot.get("provider") != "openrouter":
        raise ReferenceError("OpenRouter snapshot has an unexpected provider")
    if (
        snapshot.get("default_policy_profile") != manifest["default_policy_profile"]
        or snapshot.get("policy_profiles") != manifest["policy_profiles"]
    ):
        raise ReferenceError("OpenRouter snapshot policies do not match the manifest")
    if snapshot.get("manifest_sha256") != _sha256(_canonical_json(manifest)):
        raise ReferenceError("OpenRouter snapshot was built from a different manifest")
    expected = sorted(item["slug"] for item in manifest["models"])
    actual_models = snapshot.get("models")
    if not isinstance(actual_models, list):
        raise ReferenceError("OpenRouter snapshot models must be a list")
    actual = sorted(
        item.get("approved_slug")
        for item in actual_models
        if isinstance(item, dict) and isinstance(item.get("approved_slug"), str)
    )
    if actual != expected or len(actual_models) != len(expected):
        raise ReferenceError("OpenRouter snapshot model selection does not match")


def _print_summary(candidate: JsonObject, paths: dict[str, Path]) -> None:
    print(f"OpenRouter candidate: {paths['candidate']}")
    print(f"Review diff: {paths['diff']}")
    _print_availability(candidate)


def _print_availability(snapshot: JsonObject) -> None:
    primary_missing = snapshot.get("primary_catalog_missing") or []
    unavailable = snapshot.get("runtime_unavailable") or []
    strict_unavailable = snapshot.get("private_zdr_unavailable") or []
    if primary_missing:
        print("Approved slugs absent from /models:")
        for slug in primary_missing:
            print(f"  - {slug}")
    if unavailable:
        print("Approved slugs not runtime-ready under catalog/policy evidence:")
        for slug in unavailable:
            print(f"  - {slug}")
    if strict_unavailable:
        print("Approved slugs blocked under private_zdr (including groups):")
        for slug in strict_unavailable:
            print(f"  - {slug}")


def _projection_bytes(value: Any) -> bytes:
    if isinstance(value, str):
        return value.encode()
    return _canonical_json(value)


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode()


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _normalize_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"


def _utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(value)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _write_json(path: Path, value: Any) -> None:
    _atomic_write(path, _canonical_json(value))


def _load_optional_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    return _load_required_json(path, "snapshot")


def _load_required_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReferenceError(f"OpenRouter {label} does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ReferenceError(f"cannot load OpenRouter {label}: {path}") from exc


def _source_value(fetched: dict[str, FetchedSource], source_id: str) -> Any:
    try:
        return fetched[source_id].value
    except KeyError as exc:
        raise ReferenceError(f"required source was not fetched: {source_id}") from exc


def _required_mapping(value: Any, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ReferenceError(f"{label} must be an object")
    return value


def _required_object_value(value: Any, label: str) -> JsonObject:
    return _required_mapping(value, label)


def _required_dict(value: JsonObject, key: str) -> JsonObject:
    return _required_mapping(value.get(key), key)


def _required_list(value: JsonObject, key: str) -> list[Any]:
    item = value.get(key)
    if not isinstance(item, list):
        raise ReferenceError(f"{key} must be a list")
    return item


def _required_string(value: JsonObject, key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ReferenceError(f"{key} must be a non-empty string")
    return item


def _required_text_value(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ReferenceError(f"{label} must be text")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="tracked OpenRouter source manifest",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("refresh", help="fetch sources and write review artifacts")
    check_parser = commands.add_parser("check", help="fail on reviewed source drift")
    check_parser.add_argument(
        "--offline",
        action="store_true",
        help="compare the existing candidate without fetching",
    )
    accept_parser = commands.add_parser(
        "accept",
        help="promote an already-reviewed candidate",
    )
    accept_parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm the candidate and raw sources were reviewed",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the reference workflow."""
    args = _build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        if args.command == "refresh":
            refresh(manifest)
            return 0
        if args.command == "check":
            return 0 if check(manifest, offline=args.offline) else 1
        if args.command == "accept":
            accept(manifest, confirmed=args.yes)
            return 0
        raise AssertionError("unreachable")
    except ReferenceError as exc:
        print(f"OpenRouter reference error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
