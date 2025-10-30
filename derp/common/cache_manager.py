"""Cache manager for Gemini prompt caching."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import logfire
from aiocache import Cache
from google import genai
from google.genai import types


class PromptCacheManager:
    """Manages Gemini prompt caching with automatic TTL and invalidation."""

    def __init__(
        self,
        cache_ttl: int = 300,  # 5 minutes default
        enable_caching: bool = True,
    ) -> None:
        """Initialize cache manager.

        Args:
            cache_ttl: Time-to-live for cached content in seconds
            enable_caching: Whether caching is enabled
        """
        self.cache_ttl = cache_ttl
        self.enable_caching = enable_caching
        self._cache = Cache(Cache.MEMORY)  # In-memory cache for metadata

    def _generate_cache_key(self, system_prompt: str) -> str:
        """Generate a stable cache key from system prompt."""
        prompt_hash = hashlib.sha256(system_prompt.encode()).hexdigest()[:16]
        return f"gemini_prompt_{prompt_hash}"

    async def get_or_create_cached_content(
        self,
        client: genai.Client,
        system_prompt: str,
        model_name: str,
    ) -> str | None:
        """Get existing cached content name or create new one.

        Args:
            client: Gemini client instance
            system_prompt: The system prompt to cache
            model_name: Model name for the cache

        Returns:
            Cached content name if caching is enabled and successful, None otherwise
        """
        if not self.enable_caching:
            logfire.debug("prompt_caching_disabled")
            return None

        # System prompts must be >1024 tokens for caching
        if len(system_prompt) < 3000:  # Rough estimate: 3000 chars ≈ 1024 tokens
            logfire.debug(
                "prompt_too_short_for_caching",
                prompt_length=len(system_prompt),
            )
            return None

        cache_key = self._generate_cache_key(system_prompt)

        try:
            # Check if we have a valid cached content name
            cached_metadata = await self._cache.get(cache_key)
            if cached_metadata:
                cached_name = cached_metadata.get("name")
                expires_at = cached_metadata.get("expires_at")

                # Check if still valid (with 30s buffer)
                if expires_at and datetime.fromisoformat(expires_at) > datetime.now():
                    logfire.info(
                        "prompt_cache_hit",
                        cache_key=cache_key,
                        cached_name=cached_name,
                    )
                    return cached_name

                logfire.info("prompt_cache_expired", cache_key=cache_key)

            # Create new cached content
            cached_name = await self._create_cached_content(
                client, system_prompt, model_name, cache_key
            )
            return cached_name

        except Exception:
            logfire.exception("prompt_cache_error", cache_key=cache_key)
            return None

    async def _create_cached_content(
        self,
        client: genai.Client,
        system_prompt: str,
        model_name: str,
        cache_key: str,
    ) -> str | None:
        """Create new cached content in Gemini."""
        try:
            # Create cached content with system instruction
            cached_content = await client.aio.caches.create(
                model=model_name,
                config=types.CreateCachedContentConfig(
                    system_instruction=system_prompt,
                    ttl=f"{self.cache_ttl}s",
                ),
            )

            cached_name = cached_content.name

            # Store metadata in local cache
            expires_at = datetime.now() + timedelta(seconds=self.cache_ttl)
            await self._cache.set(
                cache_key,
                {
                    "name": cached_name,
                    "created_at": datetime.now().isoformat(),
                    "expires_at": expires_at.isoformat(),
                },
                ttl=self.cache_ttl,
            )

            logfire.info(
                "prompt_cache_created",
                cache_key=cache_key,
                cached_name=cached_name,
                ttl=self.cache_ttl,
            )

            return cached_name

        except Exception:
            logfire.exception("prompt_cache_creation_failed", cache_key=cache_key)
            return None

    async def invalidate_cache(self, system_prompt: str) -> None:
        """Invalidate cached content for a specific system prompt."""
        cache_key = self._generate_cache_key(system_prompt)
        await self._cache.delete(cache_key)
        logfire.info("prompt_cache_invalidated", cache_key=cache_key)

    async def clear_all(self) -> None:
        """Clear all cached content metadata."""
        await self._cache.clear()
        logfire.info("prompt_cache_cleared")
