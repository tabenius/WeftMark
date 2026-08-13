# Documentation and asset build policy

## Source of truth

`docs/weftmark.mdx` is the publication entrypoint and source-of-truth manifest.
It includes ordered fragments under `docs/chapters/` using deterministic
`<!-- include: ... -->` comments expanded by `scripts/build_html.py`. The MDX
subset is YAML front matter, Markdown, and raw HTML; no executable JSX or
JavaScript is required.

`docs/weftmark.css` is the shared screen + print stylesheet. Screen rules target
comfortable tablet reading; `@media print` and `@page` rules target A5 portrait.

## Historical revision 0

The files below are immutable historical artifacts copied from the first
product/architecture report:

- `docs/artifacts/weftmark_rev0.html`
- `docs/artifacts/weftmark_rev0.pdf`

Normal builds write only to `build/` and must not overwrite revisioned artifacts.
A later deliberately preserved publication should receive a new explicit suffix
(e.g. `_rev1`) rather than mutating rev0.

## HTML

```bash
python scripts/build_html.py
```

The build expands `docs/weftmark.mdx` and its chapter fragments, then Pandoc
keeps the raw layout blocks, inlines the CSS and local images with
`--embed-resources`, and writes `build/weftmark.html`.
The result is self-contained: it can be copied to a tablet without an asset
directory or network connection.

## PDF

```bash
python scripts/build_pdf.py
```

The script first rebuilds HTML, then invokes WeasyPrint against that file. The
same CSS therefore governs browser/tablet and A5 print output.

## Logos

Canonical vector sources:

- `assets/weftmark.svg`
- `assets/weftmark-on-black.svg`

Build raster derivatives with:

```bash
python scripts/build_logo.py --size 512
```

The selected raster concept is preserved at
`assets/weftmark-selected-rev0.png` only as a design reference.


## Generated figures and immutable rev0

`python scripts/build_figures.py` creates the explanatory PNG diagrams used by
`docs/weftmark.mdx`. The diagrams are generated inputs, not hand-edited source.
`make rev0` builds figures, canonical logo derivatives, the self-contained HTML,
and the A5 PDF, then snapshots the document to `docs/artifacts/weftmark_rev0.*`.
The GitHub bootstrap workflow performs this once on the first successful `main`
build and thereafter treats rev0 as immutable.

`make clean` removes only disposable files under `build/`. It deliberately
preserves the committed rev0 figures and logo derivatives; refresh those with
the explicit `figures` and `logo` targets when their canonical sources change.
