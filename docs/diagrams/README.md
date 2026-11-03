# Diagrams

Each diagram has two files:

- `<name>.mmd`: the Mermaid source. **Edit this one.**
- `<name>.svg`: the rendered image that the Markdown files display.

The docs show the SVG rather than a live Mermaid block because some Markdown viewers (including the Claude desktop app's file preview) fail to render Mermaid with the error `Cannot read properties of null (reading 'getAttribute')`, even for valid diagrams. An image renders everywhere: GitHub, MkDocs, VS Code, and the app.

## Regenerating an SVG after editing a `.mmd` file

With Node installed:

```bash
npx -p @mermaid-js/mermaid-cli mmdc -i docs/diagrams/<name>.mmd -o docs/diagrams/<name>.svg -b white
```

Without Node: paste the `.mmd` contents into [mermaid.live](https://mermaid.live), then use **Actions → SVG** to download, and save it over the old `.svg`.

Commit the `.mmd` and `.svg` together.

| Diagram | Used in |
|---|---|
| `architecture-pipeline` | [architecture.md](../../architecture.md), section 3 |
| `architecture-child-loop` | [architecture.md](../../architecture.md), section 4.3 |
| `0002-utterance-loop` | [RFC 0002](../../rfcs/0002-child-simulator-agent.md) |
| `0003-labeling-flow` | [RFC 0003](../../rfcs/0003-judge-and-evaluation.md) |
| `0009-data-flow` | [RFC 0009](../../rfcs/0009-technology-stack-and-repo.md) |
