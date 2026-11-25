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

`childbench-concept` and `childbench-relevance` are hand-written SVGs with no `.mmd` source: edit the SVG.

| Diagram | Used in |
|---|---|
| `childbench-concept` | [README](../../README.md), [architecture.md](../../architecture.md) |
| `childbench-relevance` | [README](../../README.md) |

The other diagrams (`architecture-*`, `0002-*`, `0003-*`, `0009-*`) show the earlier design and aren't used by any document now.
