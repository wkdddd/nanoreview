# Design Constraints

## Core Stays Small

New capabilities belong at extension points: channels, tools, skills, Providers or MCP servers. `AgentLoop` and `AgentRunner` are critical-path code; changes there must be minimal and justified. Runtime events may be generic, but channel and WebUI wire behavior must stay in their adapters or coordinators.

## Prefer Explicit, Local Solutions

Favor readable code over framework layers and indirection. Add abstraction only when it reduces real complexity, protects a meaningful boundary or follows an established local pattern. A tighter tool contract, channel-local adjustment or focused regression test is often the correct solution.

Channels and Providers may deliberately repeat small pieces of logic. Do not add complex base classes or shared helpers just to satisfy DRY.

## Keep Changes Reviewable

Change only what is required to solve the observed problem. State the protected invariant, use the smallest effective surface and add the closest regression test. Separate behavior changes from cleanup or refactoring.

Configuration belongs in Pydantic schema models. Fail invalid configuration clearly rather than silently repairing it. Provider resolution must remain traceable from the factory to the concrete Provider.
