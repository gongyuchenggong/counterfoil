"""Pre-render the Mermaid diagrams in DESIGN.md to SVG for the dashboard, which makes no outside requests.

Only needed after editing a diagram; a test fails until this has been run:
    uv run --with playwright python tools/render_diagrams.py     (first: uvx playwright install chromium)
"""

import hashlib
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "recon" / "diagrams"
MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@11.12.0/dist/mermaid.min.js"
# Day palette -> the dashboard's colour tokens, so one drawing follows both day and night mode.
TOKENS = {
    "#ffffff": "--card",
    "#f7f8fa": "--bg",
    "#1f2933": "--text",
    "#5e6c7b": "--muted",
    "#fdf3d8": "--tint-medium",
    "#fbe2d6": "--tint-high",
}
THEME = {
    "theme": "base",
    "startOnLoad": False,
    "flowchart": {"wrappingWidth": 340},
    "themeVariables": {
        "fontFamily": "-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif",
        "primaryColor": "#ffffff",
        "primaryTextColor": "#1f2933",
        "primaryBorderColor": "#5e6c7b",
        "secondaryColor": "#ffffff",
        "tertiaryColor": "#ffffff",
        "lineColor": "#5e6c7b",
        "textColor": "#1f2933",
        "mainBkg": "#ffffff",
        "nodeBorder": "#5e6c7b",
        "clusterBkg": "#f7f8fa",
        "clusterBorder": "#5e6c7b",
        "edgeLabelBackground": "#ffffff",
        "titleColor": "#1f2933",
    },
}


def main() -> None:
    sources = re.findall(r"```mermaid\n(.*?)```", (ROOT / "DESIGN.md").read_text(encoding="utf-8"), flags=re.S)
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("*.svg"):
        old.unlink()
    with sync_playwright() as p:
        page = p.chromium.launch().new_page()
        page.set_content("<!doctype html><body></body>")
        page.add_script_tag(url=MERMAID)
        page.evaluate("config => mermaid.initialize(config)", THEME)
        for source in sources:
            key = hashlib.sha256(source.encode()).hexdigest()[:12]
            svg = page.evaluate("([id, s]) => mermaid.render(id, s).then(r => r.svg)", [f"d{key}", source])
            svg = re.sub(r"filter:\s*drop-shadow\([^;}\"]*?\)\)?(?=[;}\"])", "filter:none", svg)  # a haze at night
            for colour, token in TOKENS.items():
                as_rgb = "rgb({}, {}, {})".format(*bytes.fromhex(colour[1:]))
                svg = svg.replace(colour, f"var({token})").replace(as_rgb, f"var({token})")
            (OUT / f"{key}.svg").write_text(svg, encoding="utf-8")
            print(key, len(svg), source.splitlines()[1].strip())


if __name__ == "__main__":
    sys.exit(main())
