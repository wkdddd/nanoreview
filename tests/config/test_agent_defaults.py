"""Config contract: the removed idle AutoCompact TTL field is gone.

The ``session_ttl_minutes`` field (aliases ``idleCompactAfterMinutes`` /
``sessionTtlMinutes``) was removed with the idle AutoCompact runtime path.
Old config files may still carry those keys; Pydantic's default ``extra``
rule ignores them instead of failing the load.
"""

from __future__ import annotations

from nanoreview.config.schema import AgentDefaults


def test_agent_defaults_load_without_the_removed_ttl_field() -> None:
    defaults = AgentDefaults()

    assert "session_ttl_minutes" not in AgentDefaults.model_fields
    # Neighbouring fields are untouched by the removal.
    assert defaults.max_messages == 120
    assert defaults.consolidation_ratio == 0.5


def test_removed_ttl_keys_are_ignored_as_unknown_fields() -> None:
    defaults = AgentDefaults(
        **{
            "idleCompactAfterMinutes": 30,
            "sessionTtlMinutes": 60,
            "session_ttl_minutes": 15,
        }
    )

    assert "session_ttl_minutes" not in defaults.model_dump()
    assert "idleCompactAfterMinutes" not in defaults.model_dump()
    assert defaults.max_messages == 120
    assert defaults.consolidation_ratio == 0.5
