"""Build a single-file offline HTML from Vite dist output.

Usage::

    python build-offline.py [--dist DIST] [--out OUT]

Defaults are derived from the script location so the script is portable
across machines — ``DIST`` defaults to ``<repo>/review-webui/dist`` and
``OUT`` defaults to ``<repo>/offline-demo/index.html``.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys
from pathlib import Path

# Derive repo root from this script's location for portability.
_REPO_ROOT = Path(__file__).resolve().parent
_DEFAULT_DIST = _REPO_ROOT / "review-webui" / "dist"
_DEFAULT_OUT = _REPO_ROOT / "offline-demo" / "index.html"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a single-file offline HTML from Vite dist output."
    )
    parser.add_argument(
        "--dist",
        type=Path,
        default=_DEFAULT_DIST,
        help=f"Path to the Vite dist directory (default: {_DEFAULT_DIST})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_OUT,
        help=f"Output HTML file path (default: {_DEFAULT_OUT})",
    )
    return parser.parse_args()


def _find_asset(
    dist: Path, html: str, pattern: str, extension: str, asset_type: str
) -> str:
    """Find an asset filename, first from the HTML reference, then by scanning.

    Raises ``RuntimeError`` if the asset cannot be found.
    """
    match = re.search(pattern, html)
    if match:
        return match.group(1).replace("./assets/", "")

    assets_dir = dist / "assets"
    if not assets_dir.is_dir():
        raise RuntimeError(
            f"Missing {asset_type} asset: no <{asset_type}> reference found in "
            f"index.html and assets directory does not exist: {assets_dir}"
        )
    files = [f for f in os.listdir(assets_dir) if f.endswith(extension)]
    if not files:
        raise RuntimeError(
            f"Missing {asset_type} asset: no .{extension.lstrip('.')} files found in "
            f"{assets_dir}"
        )
    files.sort(key=lambda f: os.path.getsize(assets_dir / f), reverse=True)
    return files[0]


def main() -> None:
    args = _parse_args()
    dist: Path = args.dist
    out: Path = args.out

    index_html = dist / "index.html"
    if not index_html.is_file():
        print(
            f"Error: {index_html} not found. Run 'cd review-webui && bun run build' first."
        )
        sys.exit(1)

    with open(index_html, "r", encoding="utf-8") as f:
        html = f.read()

    # Find the JS and CSS files referenced in the HTML
    js_file = _find_asset(
        dist, html, r'<script[^>]+src="(\./assets/[^"]+\.js)"', ".js", "JS"
    )
    css_file = _find_asset(
        dist, html, r'<link[^>]+href="(\./assets/[^"]+\.css)"', ".css", "CSS"
    )

    print(f"JS file: {js_file}")
    print(f"CSS file: {css_file}")

    with open(dist / "assets" / js_file, "r", encoding="utf-8") as f:
        js_content = f.read()

    with open(dist / "assets" / css_file, "r", encoding="utf-8") as f:
        css_content = f.read()

    # Read logo as base64
    logo_path = dist / "logo.png"
    logo_b64 = ""
    if logo_path.exists():
        with open(logo_path, "rb") as f:
            logo_b64 = base64.b64encode(f.read()).decode("ascii")

    # Escape </script> in JS content to prevent premature script tag closing
    js_content = js_content.replace("</script>", "<\\/script>")

    # Build the final HTML
    # Key changes:
    # 1. Remove all external CDN links (Google Fonts, preconnect)
    # 2. Replace favicon with data URI (both /logo.png and ./logo.png forms)
    # 3. Remove the <script type="module" src="..."> from <head>
    # 4. Add CSS as <style> in <head>
    # 5. Add JS as <script type="module"> at the END of <body> (after #root div)
    #    Inline module scripts work with file:// protocol (no CORS issue)

    # Remove Google Fonts and preconnect
    html = re.sub(r'<link rel="preconnect"[^>]*/?>', "", html)
    html = re.sub(r'<link href="https://fonts\.googleapis\.com[^"]*"[^>]*/?>', "", html)

    # Replace favicon with data URI or remove
    if logo_b64:
        data_uri = f"data:image/png;base64,{logo_b64}"
        # Replace all logo references (both ./logo.png and /logo.png)
        html = html.replace("./logo.png", data_uri)
        html = html.replace("/logo.png", data_uri)
    else:
        html = re.sub(r'<link rel="icon"[^>]*/?>', "", html)
        html = re.sub(r'<link rel="apple-touch-icon"[^>]*/?>', "", html)

    # Remove the external script tag from head
    html = re.sub(r'<script type="module"[^>]*src="[^"]*"[^>]*></script>', "", html)

    # Remove external CSS link
    html = re.sub(r'<link rel="stylesheet"[^>]*href="[^"]*\.css"[^>]*/?>', "", html)

    # Add CSS as inline style in head (before </head>)
    css_tag = f"<style>\n{css_content}\n</style>"
    html = html.replace("</head>", f"{css_tag}\n</head>")

    # Add JS as inline module script at end of body (after #root div, before </body>)
    # Using type="module" because Vite output uses ES module syntax
    js_tag = f'<script type="module">\n{js_content}\n</script>'
    html = html.replace("</body>", f"{js_tag}\n</body>")

    # Write output
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)

    size = out.stat().st_size
    print(f"Output: {out}")
    print(f"Size: {size} bytes ({size / 1024 / 1024:.2f} MB)")


if __name__ == "__main__":
    main()
