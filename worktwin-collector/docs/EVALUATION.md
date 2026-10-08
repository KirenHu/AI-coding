# WorkTwin evaluation gate

## Scope

This is a reproducible **functional/security contract suite**, not a claim of answer correctness.
It uses synthetic project notes only. It exercises actual local file collection,
SQLite, local FastAPI routes, twin knowledge authorization, and answer citation checks.

Run from `worktwin-collector/`:

```bash
pip install -e '.[test]'
python scripts/evaluate_twin.py --mode mock --output local-eval.json

# Optional. May incur BYOK costs. Sends only synthetic fixtures.
WORKTWIN_GATEWAY_URL=https://your-enterprise-gateway.example \
WORKTWIN_GATEWAY_TOKEN=your_access_token \
python scripts/evaluate_twin.py --mode gateway --output gateway-eval.json
```

Never add enterprise credentials to the repo, committed shell scripts or shared screenshots.
Inject secrets via a trusted environment/secret manager. The JSON report includes
only per-check booleans and pass rates; it never stores prompts, source text,
provider tokens or model responses.

## Checks

- Authorized knowledge is usable and returned with a known [K-id] citation.
- Private, unassigned content never enters an outgoing model context.
- A twin with no assigned knowledge cannot access another twin's articles.
- Review-held knowledge cannot be used despite historical assignment.
- Removing a source invalidates its dependent twin knowledge.
- Unknown/fabricated source IDs or uncited substantive assertions are blocked.
- The model may state that the available evidence is insufficient.

Note: `answer_status=cited` proves only that the citation identifier belongs
to the authorized model context. It does **not** prove semantic support for the
claims. A hallucination could still attach a valid citation.

The mock suite uses a deterministic fake model and is free. Gateway mode uses the
real enterprise BYOK connector, with synthetic prompts; it may cost money.
Neither mode estimates real-workplace factual accuracy. Before production use,
build a privately held, human-labeled golden dataset; evaluate recall, grounded
claims, abstention, historical-decision accuracy and model changes separately.
Keep the dataset out of this public repository.

## Inspiration, open source and attribution

| Project | What inspired this version | Code reused |
|---|---|---|
| [Langfuse Experiments](https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk) | Dataset → evaluation run → per-item score → CI regression gate | **None**. A small offline runner is implemented independently. |
| [Ragas Faithfulness](https://github.com/vibrantlabsai/ragas/blob/main/docs/concepts/metrics/available_metrics/faithfulness.md) | Explicitly separate structural citations from actual claim-level grounding | **None**. Faithfulness score is not computed. |
| [Rowboat](https://github.com/rowboatlabs/rowboat) | Earlier design direction of knowledge-first, browsable personal memory | 评测器未复用代码；1.0 的 Markdown 解析有小范围 Apache-2.0 移植，见 OSS.md。 |

Actual runtime open-source libraries **used as dependencies**: FastAPI and
Starlette TestClient, SQLite, Python standard library; the collector already
uses watchfiles, pypdf and python-docx. These are installed as dependencies,
not vendored or copied from a competing application's source.

## Explicit non-goals

The evaluation suite does not verify enterprise identity, centralized RBAC,
source ACL inheritance, DLP, signed installers, or full LLM-as-a-judge scoring.
1.0 adds remote digital-twin capability URLs, covered by separate sharing tests.
The local session token is not enterprise SSO.
