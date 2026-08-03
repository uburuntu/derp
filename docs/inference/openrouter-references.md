# OpenRouter catalog references

`openrouter-sources.json` is the tracked source and model-selection manifest.
`openrouter-reviewed.json` is the last reviewed projection of those sources.
Neither file is a runtime catalog: runtime enablement still requires explicit
typed catalog and adapter work.

## Refresh

Run from the repository root:

```sh
uv run python scripts/openrouter_references.py refresh
```

The command uses only public, unauthenticated `https://openrouter.ai` sources.
It writes exact responses, fetch metadata, a normalized candidate, and a unified
review diff under the gitignored `references/openrouter/` directory:

```text
references/openrouter/
|-- raw/
|-- fetch-metadata.json
|-- openrouter-reviewed.candidate.json
`-- openrouter-reviewed.diff
```

The projection covers the primary model catalog, dedicated image and video
catalogs, stable per-provider endpoint facts, the ZDR endpoint set, relevant
OpenAPI paths, and the routing/privacy/multimodal documentation named in the
manifest. Dynamic endpoint health, latency, and throughput are retained in raw
responses but excluded from the reviewed projection.

Each source records its URL, retrieval time, exact-response SHA-256, byte count,
and deterministic review-projection SHA-256. Retrieval time and the exact raw
hash are evidence but do not create drift by themselves; `check` gates on the
normalized review hash and model facts.

## Check

Fetch current sources and compare them with the reviewed baseline:

```sh
uv run python scripts/openrouter_references.py check
```

Use the existing candidate without network access only when diagnosing a prior
refresh:

```sh
uv run python scripts/openrouter_references.py check --offline
```

The command exits non-zero when pricing, canonical slugs, capabilities,
provider/ZDR facts, relevant contracts, or selected documentation drift. It
also lists every approved slug missing from the primary catalog. Dedicated-only,
documented-only, and unavailable entries remain explicit; they must not be
treated as executable runtime support.

Catalog presence is not policy readiness. Every model records its selected
policy profile plus strict-ZDR readiness. Paid and group execution use
`private_zdr`. The three approved free models use `consented_non_zdr_free` only
after versioned terms/privacy consent and only in private or inline contexts;
they remain blocked in groups and under strict ZDR. A dedicated model with no
provider-to-ZDR mapping is unverified and must fail closed.

Runtime chat requests never inherit changing upstream reasoning defaults.
Optional ordinary and free roles disable reasoning, `CHAT_REASONING` explicitly
uses high effort, and the catalog-mandatory `CHAT_MULTIMODAL` model explicitly
uses minimal effort.

## Review and accept

Inspect the ignored raw responses, candidate, and diff. Verify at least:

- every changed price and unit;
- canonical slug and context/output limits;
- required modalities, parameters, and dedicated API support;
- provider tags and availability under ZDR;
- any model absent from the primary catalog;
- routing, data-collection, and fallback documentation changes.

After review, promote the exact current candidate:

```sh
uv run python scripts/openrouter_references.py accept --yes
uv run python scripts/openrouter_references.py check --offline
```

Commit only the manifest, reviewed snapshot, tooling, tests, and this document.
Never force-add files under `references/openrouter/`.
