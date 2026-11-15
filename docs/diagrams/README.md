# README diagrams / README 图表 / README の図

The Mermaid code inside `README.md`, `README.en.md`, and `README.ja.md` is the
**authoritative source**. Each README displays two generated PNGs and keeps the
corresponding source inside a `<details>` block. Edit that source, then regenerate
all six images. Do not edit the PNGs or `manifest.json` by hand.

三语 README 中的 Mermaid 代码是唯一修改依据。正文显示 PNG，折叠区保留代码；
修改代码后重新生成图片，不单独改图片或校验清单。

三言語の README 内の Mermaid ソースを正とします。本文には PNG を表示し、
ソースは折りたたみ内に保持します。ソース変更後は画像を再生成し、PNG や
チェック用 manifest を手動で書き換えないでください。

## Generate

From the repository root, with Node.js 22 or newer:

```bash
npm ci --prefix docs/diagrams
npm run build --prefix docs/diagrams
npm run check --prefix docs/diagrams
```

The isolated, locked development dependencies in this directory provide the
Mermaid renderer. They are not Python runtime or Vercel console dependencies.
Installation normally obtains Puppeteer's browser. To reuse an installed browser:

```bash
PUPPETEER_SKIP_DOWNLOAD=true npm ci --prefix docs/diagrams
PUPPETEER_EXECUTABLE_PATH="/path/to/chrome" npm run build --prefix docs/diagrams
```

For example, macOS Google Chrome is normally located at
`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`.
Install CJK fonts if your system lacks them; the theme requests Noto Sans CJK,
with macOS Hiragino fallbacks. Font/browser differences can change pixel output.
Review all language images after regenerating them, particularly line wrapping
and Chinese/Japanese glyphs.

## Source and output mapping

| Source | Architecture | Execution |
| --- | --- | --- |
| `README.md` | `architecture.zh.png` | `execution.zh.png` |
| `README.en.md` | `architecture.en.png` | `execution.en.png` |
| `README.ja.md` | `architecture.ja.png` | `execution.ja.png` |

`<!-- rp-diagram: architecture.zh -->` and equivalent markers identify each block.
Keep those markers and the fenced `mermaid` language when editing. Rendering
style lives in `mermaid.config.json`; diagram content remains in the READMEs.

The generator uses a white background and a 1.5x output scale. `manifest.json`
records source hashes, build-input hashes, PNG hashes, dimensions and byte sizes.
The check requires no browser or npm installation:

```bash
node scripts/build_readme_diagrams.mjs --check
```

CI checks the source/image correspondence without re-rendering on a different
platform. Changing source, renderer settings, locked dependencies, or the build
script requires regeneration. Image changes without an updated source-bound
manifest also fail. Unrelated README prose edits do not require new PNGs.

The public-tree audit allows only these six exact PNG paths, with matching hashes
and bounded dimensions/sizes. Its restrictions on other binary/private files
remain in place. Commit the edited README sources, generated images, and manifest
together. Regeneration does not access private character repositories or model
services.
