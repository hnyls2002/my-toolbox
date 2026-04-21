"""Local markdown preview server with syntax highlighting.

Renders markdown to HTML using python-markdown + pygments.
Auto-reloads file on each request. Intended to be launched as a background process.

Usage:
    python -m my_toolbox.grip.server <file> <port>
"""

import re
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import markdown
from pygments.formatters import HtmlFormatter

GITHUB_CSS_URL = "https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.6.1/github-markdown-light.min.css"

MD_EXTENSIONS = [
    "fenced_code",
    "codehilite",
    "tables",
    "toc",
    "nl2br",
    "sane_lists",
    "smarty",
    "attr_list",
    "md_in_html",
]

MD_EXTENSION_CONFIGS = {
    "codehilite": {"css_class": "highlight", "guess_lang": True},
}

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="{github_css}">
<style>
  body {{
    box-sizing: border-box;
    min-width: 200px;
    max-width: 980px;
    margin: 0 auto;
    padding: 45px;
    background: #fff;
  }}
  .markdown-body {{ font-size: 16px; }}
  @media (max-width: 767px) {{
    body {{ padding: 15px; }}
  }}
  {pygments_css}
</style>
</head>
<body>
<article class="markdown-body">
{content}
</article>
</body>
</html>
"""


def _extract_title(text: str, fallback: str) -> str:
    """Extract the first markdown heading as page title."""
    m = re.search(r"^#{1,3}\s+(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else fallback


def render_markdown(file_path: Path) -> str:
    """Read and render a markdown file to HTML."""
    text = file_path.read_text(encoding="utf-8")
    title = _extract_title(text, file_path.name)
    md = markdown.Markdown(
        extensions=MD_EXTENSIONS,
        extension_configs=MD_EXTENSION_CONFIGS,
    )
    body = md.convert(text)
    pygments_css = HtmlFormatter().get_style_defs(".highlight")
    return HTML_TEMPLATE.format(
        title=title,
        github_css=GITHUB_CSS_URL,
        content=body,
        pygments_css=pygments_css,
    )


def make_handler(file_path: Path):
    """Create a request handler that serves the rendered markdown and
    static assets (images, etc.) from the markdown file's parent directory."""

    md_dir = str(file_path.parent)
    md_name = file_path.name

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=md_dir, **kwargs)

        def do_GET(self):
            path = self.path.split("?", 1)[0].split("#", 1)[0]
            if path == "/" or path.lstrip("/") == md_name:
                html = render_markdown(file_path)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
                return
            super().do_GET()

        def log_message(self, format, *args):
            pass  # suppress request logs

    return Handler


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <file> <port> [host]", file=sys.stderr)
        sys.exit(1)

    file_path = Path(sys.argv[1]).resolve()
    port = int(sys.argv[2])
    host = sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1"

    server = HTTPServer((host, port), make_handler(file_path))
    server.serve_forever()


if __name__ == "__main__":
    main()
