# Diagrams

Rendered PNGs of every diagram in the docs, so a human can *see* how LeadForge works without reading code.
Sources are the `.mmd` files beside each image (and inline mermaid in the docs they belong to).

Regenerate after editing a `.mmd`:

```bash
npm install -g @mermaid-js/mermaid-cli
mmdc -i 01-how-it-works.mmd -o 01-how-it-works.png -b white -w 1600
```

| Image | What it shows | Doc |
|---|---|---|
| `01-how-it-works.png` | **Start here.** The whole thing in 5 steps, plain language | [README](../../README.md) |
| `02-architecture.png` | Who talks to what: operator → agent → CLI → internet | [docs/02](../02-architecture.md) |
| `03-pipeline-sequence.png` | A full campaign as a timeline of calls | [docs/04](../04-pipeline-behavior.md) |
| `04-run-states.png` | Run stages and where a run can resume from | [docs/04](../04-pipeline-behavior.md) |
| `05-icm-stages.png` | Build stages + gates, colored by status (green done, yellow partial, orange to-do) | [docs/05](../05-icm-build-plan.md) |
| `06-data-model.png` | What gets stored and how it links together | [docs/03](../03-data-model.md) |
| `07-token-economics.png` | Why this design costs ~15k tokens instead of hundreds of thousands | [docs/06](../06-token-contract.md) |
