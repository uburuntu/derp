"""Process bootstrap for the Telegram bot."""

from __future__ import annotations

import asyncio
import logging
import sys

from derp import __version__
from derp.observability import (
    Observability,
    ObservabilityConfig,
    configure_observability,
    enable_early_pydantic_instrumentation,
    report_exception,
    shutdown_observability,
)


def main() -> None:
    """Configure process-wide services, run the app, and flush telemetry."""
    enable_early_pydantic_instrumentation()

    from derp.config import settings

    capture_ai_content = (
        settings.environment == "dev" and settings.logfire_capture_ai_content
    )
    observability = configure_observability(
        ObservabilityConfig(
            service_name=settings.app_name,
            service_version=__version__,
            environment=settings.environment,
            token=settings.logfire_token.get_secret_value(),
            capture_ai_content=capture_ai_content,
        )
    )

    try:
        from derp.application import run_application

        asyncio.run(run_application(settings, observability.logfire))
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("application_stopped")
    except Exception as exc:
        report_exception(
            "application_crashed",
            exception=exc,
            logfire_instance=observability.logfire,
        )
        raise
    finally:
        _shutdown_observability(observability)


def _shutdown_observability(observability: Observability) -> None:
    if shutdown_observability(observability) is False:
        sys.stderr.write("Timed out while shutting down telemetry\n")


if __name__ == "__main__":
    main()
