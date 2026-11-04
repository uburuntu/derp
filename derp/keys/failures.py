"""How a run a contributed key paid can fail, and what that says about the key.

Only the run's own outcome is read (status codes and error codes, never logged): the provider refusing
the key itself (401/403) means it stopped working; a payment error, or a rate limit that names credits
or quota, means its credits ran out; an explicit refusal is the model's. Anything else (a timeout, a
server error, an ordinary rate limit) says nothing about the key. Every one of them, before anything was
delivered, is answered again on the chat's usual funding.
"""

from dataclasses import dataclass
from typing import Literal

from pydantic_ai.exceptions import ModelHTTPError

type FailureKind = Literal["dead", "exhausted", "refused", "failed"]
OUT_OF_CREDIT = ("quota", "credit", "balance", "billing", "insufficient")


@dataclass(frozen=True, slots=True)
class KeyFailure:
    kind: FailureKind
    key_id: int
    said: str = ""  # a refusal's text, shown in the chat (rate-limited)


def classify(error: BaseException) -> FailureKind:
    if not isinstance(error, ModelHTTPError):
        return "failed"
    body = str(error.body).lower()
    if error.status_code == 403 and ("moderation" in body or "flagged" in body):
        return "refused"  # OpenRouter's own moderation, not the key
    if error.status_code in (401, 403):
        return "dead"
    if error.status_code == 402:
        return "exhausted"
    if error.status_code == 429 and any(word in body for word in OUT_OF_CREDIT):
        return "exhausted"
    return "failed"
