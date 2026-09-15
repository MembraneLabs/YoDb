# YoDb docs site

This is a standalone, dependency-free static documentation site. It renders the
repository's [`docs/`](../docs/) MDX files; it does not own a second copy of
the documentation and has no connection to Mintlify or the YoDb runtime.

## Build and preview

From the repository root:

```bash
python3 docs-site/build.py
python3 -m http.server --directory docs-site/dist 8000
```

Open <http://localhost:8000>. The build copies the source MDX files into the
generated `docs-site/dist/` directory, which is intentionally ignored by Git.

## Navigation

Edit [`site.config.json`](site.config.json) to add or reorder navigation. Each
entry must point to an existing `docs/<path>.mdx` file; the build fails when it
does not. Keep documentation prose in `docs/`, not in this module.

## GitHub Pages

The repository workflow at [`.github/workflows/docs-site.yml`](../.github/workflows/docs-site.yml)
builds this module and deploys the generated static files to GitHub Pages after
a push to `main`. Enable **GitHub Actions** as the Pages source in the GitHub
repository settings once.
