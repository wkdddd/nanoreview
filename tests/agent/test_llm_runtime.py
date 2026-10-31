"""Immutable role runtimes: capture, field-level overrides, provider switching.

A run must be pinned to one provider/model/generation/window from admission to
its last iteration. These tests pin that:

- ``capture`` copies the provider's generation instead of aliasing it, so a
  concurrent ``/model`` switch cannot change an already admitted run;
- role overrides are per-field: an unset field inherits the conversation agent;
- switching provider requires it explicitly and goes through the injected
  builder (credentials come from the provider factory);
- ``describe`` stays bounded and persistable for the run snapshot.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanoreview.agent.llm_runtime import (
    ROLE_CONVERSATION,
    ROLE_JUDGE,
    ROLE_REVIEWER,
    LLMRuntime,
    RoleModelOverrides,
    resolve_role_runtime,
)
from nanoreview.providers.base import GenerationSettings, LLMProvider, LLMResponse


class Provider(LLMProvider):
    def __init__(self, *, max_tokens: int = 4_096, temperature: float = 0.7) -> None:
        super().__init__()
        self.generation = GenerationSettings(
            temperature=temperature, max_tokens=max_tokens
        )

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "base-model"


def _base(**generation) -> LLMRuntime:
    return LLMRuntime.capture(
        Provider(**generation),
        model="base-model",
        context_window_tokens=200_000,
        model_preset="default",
    )


# --- capture ---------------------------------------------------------------


def test_capture_copies_the_generation_settings() -> None:
    provider = Provider(max_tokens=1_024, temperature=0.2)

    runtime = LLMRuntime.capture(
        provider, model="m", context_window_tokens=64_000, role=ROLE_REVIEWER
    )
    provider.generation = GenerationSettings(max_tokens=8_192, temperature=1.0)

    assert runtime.model == "m"
    assert runtime.role == ROLE_REVIEWER
    assert runtime.context_window_tokens == 64_000
    # The snapshot did not follow the provider.
    assert runtime.max_tokens == 1_024
    assert runtime.temperature == 0.2


def test_capture_survives_a_provider_without_generation_settings() -> None:
    runtime = LLMRuntime.capture(
        SimpleNamespace(), model="m", context_window_tokens=1_000
    )

    assert runtime.generation == GenerationSettings()
    assert runtime.context_window_tokens == 1_000


def test_capture_never_yields_a_negative_window() -> None:
    runtime = LLMRuntime.capture(Provider(), model="m", context_window_tokens=-5)

    assert runtime.context_window_tokens == 0


def test_max_tokens_is_zero_when_unset() -> None:
    runtime = LLMRuntime.capture(Provider(), model="m", context_window_tokens=1_000)
    with_zero = runtime.with_generation(max_tokens=0)

    assert runtime.max_tokens == 4_096
    assert with_zero.max_tokens == 0


def test_describe_is_bounded_and_json_safe() -> None:
    runtime = LLMRuntime.capture(
        Provider(max_tokens=2_048, temperature=0.1),
        model="m",
        context_window_tokens=64_000,
        role=ROLE_JUDGE,
        provider_name="custom",
        model_preset="fast",
    )

    assert runtime.describe() == {
        "role": ROLE_JUDGE,
        "provider": "custom",
        "model": "m",
        "model_preset": "fast",
        "context_window_tokens": 64_000,
        "max_tokens": 2_048,
        "temperature": 0.1,
        "reasoning_effort": None,
    }


# --- overrides -------------------------------------------------------------


def test_unset_override_fields_inherit_the_conversation_runtime() -> None:
    base = _base()

    resolved = resolve_role_runtime(base, role=ROLE_REVIEWER, overrides=RoleModelOverrides())

    assert resolved.model == base.model
    assert resolved.context_window_tokens == base.context_window_tokens
    assert resolved.generation == base.generation
    assert resolved.role == ROLE_REVIEWER
    # Same provider object by reference; providers carry client state.
    assert resolved.provider is base.provider


def test_overrides_apply_per_field() -> None:
    base = _base(max_tokens=4_096, temperature=0.7)

    resolved = resolve_role_runtime(
        base,
        role=ROLE_JUDGE,
        overrides=RoleModelOverrides(
            model="judge-model", context_window_tokens=32_000, max_tokens=512
        ),
    )

    assert resolved.model == "judge-model"
    assert resolved.context_window_tokens == 32_000
    assert resolved.max_tokens == 512
    # Untouched fields keep the inherited values.
    assert resolved.temperature == 0.7
    assert resolved.provider is base.provider


def test_a_null_override_does_not_erase_the_inherited_value() -> None:
    base = _base(temperature=0.3)

    resolved = resolve_role_runtime(
        base, role=ROLE_REVIEWER, overrides=RoleModelOverrides(temperature=None)
    )

    assert resolved.temperature == 0.3


def test_switching_provider_requires_a_builder() -> None:
    """A cross-provider override cannot silently reuse the old provider."""
    base = _base()

    with pytest.raises(ValueError, match="provider"):
        resolve_role_runtime(
            base,
            role=ROLE_REVIEWER,
            overrides=RoleModelOverrides(provider="other"),
            provider_builder=None,
        )


def test_a_model_only_override_needs_no_builder() -> None:
    base = _base()

    resolved = resolve_role_runtime(
        base, role=ROLE_REVIEWER, overrides=RoleModelOverrides(model="m2")
    )

    assert resolved.model == "m2"
    assert resolved.provider is base.provider


def test_a_provider_override_goes_through_the_injected_builder() -> None:
    base = _base()
    other = Provider(max_tokens=900)
    built: list[tuple[str, str]] = []

    def _builder(provider_name: str, model: str):
        built.append((provider_name, model))
        return other, provider_name

    resolved = resolve_role_runtime(
        base,
        role=ROLE_REVIEWER,
        overrides=RoleModelOverrides(provider="custom", model="other-model"),
        provider_builder=_builder,
    )

    assert built == [("custom", "other-model")]
    assert resolved.provider is other
    assert resolved.provider_name == "custom"
    assert resolved.model == "other-model"


# --- config binding --------------------------------------------------------


def test_from_settings_reads_a_role_block() -> None:
    settings = SimpleNamespace(
        provider="custom",
        model="m",
        context_window_tokens=16_384,
        max_tokens=2_048,
        temperature=0.0,
        reasoning_effort="low",
    )

    overrides = RoleModelOverrides.from_settings(settings)

    assert overrides.provider == "custom"
    assert overrides.temperature == 0.0
    assert overrides.reasoning_effort == "low"
    assert not overrides.is_empty


def test_an_empty_or_missing_block_is_no_override() -> None:
    assert RoleModelOverrides.from_settings(None).is_empty
    assert RoleModelOverrides.from_settings(SimpleNamespace()).is_empty


def test_reasoning_effort_and_temperature_reach_the_generation() -> None:
    runtime = resolve_role_runtime(
        _base(),
        role=ROLE_REVIEWER,
        overrides=RoleModelOverrides(temperature=0.0, reasoning_effort="high"),
    )

    assert runtime.temperature == 0.0
    assert runtime.reasoning_effort == "high"
    # The frozen role keeps its own identity for logs and snapshots.
    assert runtime.role == ROLE_REVIEWER
    assert ROLE_CONVERSATION != ROLE_REVIEWER
