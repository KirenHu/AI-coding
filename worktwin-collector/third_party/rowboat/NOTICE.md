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
