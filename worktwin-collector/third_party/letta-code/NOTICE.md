# Letta Code workflow reference

Upstream: https://github.com/letta-ai/letta-code
Revision: 145d7cd784f2945673f9023d2bec806e641b9a05
License: Apache-2.0 (retained in LICENSE).

WorkTwin adapts the reflection and explicit-integration workflow described and
implemented in src/agent/subagents/builtin/reflection.md,
src/cli/helpers/reflection-integration.ts and reflection-launcher.ts.
The Python editor is independently implemented for SQLite and existing source
permissions, rather than importing the Letta runtime, prompts, or Git worktrees.
See docs/KNOWLEDGE_EDITOR_RESEARCH.md for the exact boundaries and evidence.
