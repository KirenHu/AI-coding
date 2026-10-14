# Rowboat Markdown index parser adaptation

Upstream: https://github.com/rowboatlabs/rowboat

Source revision: `f07c3fcd7793e99d850ee61363a91eafa16d6afb`

Source file: `apps/x/packages/core/src/knowledge/knowledge_index.ts`

License: Apache License 2.0 (full text in LICENSE).

WorkTwin ports the `extractField`, `extractList`, and `extractTitle` field parsing
logic into `worktwin/rowboat_markdown.py`. The parser is translated to Python,
uses escaped field names, and does not include Rowboat's filesystem indexing,
Electron app, Node runtime, or isomorphic-git dependency. The port supplies
Markdown aliases/keywords to WorkTwin's knowledge retrieval and vault parsing.
Original WorkTwin code remains MIT; this adapted module is Apache-2.0.

## Article maintenance workflow study (2026-10-13)

Additional studied revision: `68d8d1c8b3bb618df7643dadc00ae4f592a4fb7c`.
Sources: `apps/x/packages/core/src/knowledge/note_creation.ts`,
`note_curation.ts`, `build_graph.ts`.
WorkTwin independently adapts directory-first canonical identity and periodic
whole-note curation to its Python/SQLite article pipeline. It does not import
Rowboat's contacts, email gates, runtime, or fixed curation thresholds.
See `docs/KNOWLEDGE_EDITOR_RESEARCH.md` for evidence and limitations.
