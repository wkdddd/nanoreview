"""Immutable model execution snapshots.

A single run (conversation turn, reviewer task, Judge batch, Dream) must use one
captured provider/model/generation configuration from start to finish. The
provider object itself is stateful (clients, retry bookkeeping) and is therefore
shared by reference; every *selection* and *generation* value is copied into
:class:`LLMRuntime` so a concurrent ``/model`` switch, a config reload or another
role mutating ``provider.generation`` cannot change an already admitted run.

Role overrides (``review.reviewer`` / ``review.judge``) are field-level: an
unset field inherits the conversation agent's effective value at review start.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable

from nanoreview.providers.base import GenerationSettings, LLMProvider

#: Roles that can hold their own runtime. Used for logs and run snapshots.
ROLE_CONVERSATION = "conversation"
ROLE_REVIEWER = "reviewer"
ROLE_JUDGE = "judge"
ROLE_DREAM = "dream"


@dataclass(frozen=True, slots=True)
class LLMRuntime:
    """One frozen provider/model/generation selection for an entire execution."""

    provider: LLMProvider
    model: str
    generation: GenerationSettings
    context_window_tokens: int
    role: str = ROLE_CONVERSATION
    provider_name: str | None = None
    model_preset: str | None = None

    @classmethod
    def capture(
        cls,
        provider: LLMProvider,
        *,
        model: str,
        context_window_tokens: int,
        role: str = ROLE_CONVERSATION,
        provider_name: str | None = None,
        model_preset: str | None = None,
    ) -> "LLMRuntime":
        """Copy the provider's current generation values into a frozen snapshot."""
        generation = getattr(provider, "generation", None)
        if not isinstance(generation, GenerationSettings):
            generation = GenerationSettings()
        window = context_window_tokens if isinstance(context_window_tokens, int) else 0
        return cls(
            provider=provider,
            model=model or "",
            generation=generation,
            context_window_tokens=max(0, window),
            role=role,
            provider_name=provider_name,
            model_preset=model_preset,
        )

    @property
    def max_tokens(self) -> int:
        """Reserved output tokens for this role (0 when unset/invalid)."""
        value = self.generation.max_tokens
        return int(value) if isinstance(value, int) and value > 0 else 0

    @property
    def temperature(self) -> float | None:
        return self.generation.temperature

    @property
    def reasoning_effort(self) -> str | None:
        return self.generation.reasoning_effort

    def describe(self) -> dict[str, Any]:
        """Bounded, persistable description of the effective parameters."""
        return {
            "role": self.role,
            "provider": self.provider_name,
            "model": self.model,
            "model_preset": self.model_preset,
            "context_window_tokens": self.context_window_tokens,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "reasoning_effort": self.reasoning_effort,
        }

    def with_generation(self, **fields: Any) -> "LLMRuntime":
        """Return a derived runtime; ``None`` keeps the inherited value."""
        generation = replace(
            self.generation,
            **{key: value for key, value in fields.items() if value is not None},
        )
        return replace(self, generation=generation)


@dataclass(frozen=True, slots=True)
class RoleModelOverrides:
    """Optional field-level overrides declared by a review role's config."""

    provider: str | None = None
    model: str | None = None
    context_window_tokens: int | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None

    @classmethod
    def from_settings(cls, settings: Any) -> "RoleModelOverrides":
        """Read a config object (``ReviewReviewerSettings``/``ReviewJudgeSettings``)."""

        def _get(name: str) -> Any:
            return getattr(settings, name, None) if settings is not None else None

        return cls(
            provider=_get("provider"),
            model=_get("model"),
            context_window_tokens=_get("context_window_tokens"),
            max_tokens=_get("max_tokens"),
            temperature=_get("temperature"),
            reasoning_effort=_get("reasoning_effort"),
        )

    @property
    def is_empty(self) -> bool:
        return all(
            getattr(self, name) is None
            for name in (
                "provider",
                "model",
                "context_window_tokens",
                "max_tokens",
                "temperature",
                "reasoning_effort",
            )
        )


#: Builder signature: ``(provider_name, model) -> (provider, provider_name)``.
ProviderBuilder = Callable[[str, str], "tuple[LLMProvider, str | None]"]


def resolve_role_runtime(
    base: LLMRuntime,
    *,
    role: str,
    overrides: RoleModelOverrides | None = None,
    provider_builder: ProviderBuilder | None = None,
) -> LLMRuntime:
    """Derive a role runtime from the conversation runtime plus field overrides.

    Only the fields a role actually declares change; everything else keeps the
    value the conversation agent had when the review started. Switching provider
    requires naming it explicitly (``overrides.provider``) — a bare model name
    keeps the inherited provider, matching how presets behave elsewhere.
    """
    overrides = overrides or RoleModelOverrides()
    model = overrides.model or base.model
    provider = base.provider
    provider_name = base.provider_name
    if overrides.provider:
        if provider_builder is None:
            raise ValueError(
                f"{role} role config selects provider {overrides.provider!r} but no "
                "provider factory is available for this coordinator"
            )
        provider, provider_name = provider_builder(overrides.provider, model)

    generation = GenerationSettings(
        temperature=(
            overrides.temperature
            if overrides.temperature is not None
            else base.generation.temperature
        ),
        max_tokens=(
            overrides.max_tokens
            if overrides.max_tokens is not None
            else base.generation.max_tokens
        ),
        reasoning_effort=(
            overrides.reasoning_effort
            if overrides.reasoning_effort is not None
            else base.generation.reasoning_effort
        ),
    )
    window = (
        overrides.context_window_tokens
        if overrides.context_window_tokens is not None
        else base.context_window_tokens
    )
    return LLMRuntime(
        provider=provider,
        model=model,
        generation=generation,
        context_window_tokens=max(0, int(window or 0)),
        role=role,
        provider_name=provider_name,
        model_preset=base.model_preset,
    )
