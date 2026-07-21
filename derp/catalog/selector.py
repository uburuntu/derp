"""Deterministic model selection from feature, funding, and media shape."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from derp.catalog.google import ModelCapability, ModelRole, ModelSpec
from derp.catalog.openrouter import get_openrouter_model


class InputModality(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    PDF = "pdf"


_CAPABILITY_BY_MODALITY = {
    InputModality.TEXT: ModelCapability.TEXT_INPUT,
    InputModality.IMAGE: ModelCapability.IMAGE_INPUT,
    InputModality.AUDIO: ModelCapability.AUDIO_INPUT,
    InputModality.VIDEO: ModelCapability.VIDEO_INPUT,
    InputModality.PDF: ModelCapability.PDF_INPUT,
}


@dataclass(frozen=True, slots=True)
class ChatSelection:
    """Inputs that affect chat model selection before media is downloaded."""

    paid: bool
    free_mode_allowed: bool = False
    modalities: frozenset[InputModality] = frozenset({InputModality.TEXT})

    def __post_init__(self) -> None:
        if not self.modalities:
            raise ValueError("At least one input modality is required")


class ModelSelector:
    """Select one pinned model and fail closed when it lacks a capability."""

    def select_chat(self, selection: ChatSelection) -> ModelSpec:
        if selection.free_mode_allowed:
            role = self._free_role(selection.modalities)
        elif selection.paid and selection.modalities & {
            InputModality.AUDIO,
            InputModality.VIDEO,
        }:
            role = ModelRole.CHAT_MULTIMODAL
        elif selection.paid:
            role = ModelRole.CHAT_STANDARD
        else:
            role = ModelRole.CHAT_ECONOMY

        model = get_openrouter_model(role)
        missing = {
            _CAPABILITY_BY_MODALITY[modality] for modality in selection.modalities
        } - model.capabilities
        if missing:
            names = ", ".join(sorted(capability.value for capability in missing))
            raise ValueError(f"{model.provider_model_id} lacks {names}")
        return model

    @staticmethod
    def _free_role(modalities: frozenset[InputModality]) -> ModelRole:
        if InputModality.VIDEO in modalities:
            raise ValueError("No reviewed free model supports video input")
        if InputModality.AUDIO in modalities:
            return ModelRole.FREE_AUDIO
        if modalities & {InputModality.IMAGE, InputModality.PDF}:
            return ModelRole.FREE_VISUAL
        return ModelRole.FREE_TEXT
