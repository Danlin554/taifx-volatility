import os
import pathlib
import jinja2

_TEMPLATES_DIR = pathlib.Path(__file__).parent.parent / "templates"
_OUT_DIR = pathlib.Path(__file__).parent.parent / "data"

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=jinja2.select_autoescape(["html"]),
)


def render(snapshot: dict) -> str:
    """渲染 index.html.j2 → 回傳 HTML 字串，並寫出到 data/index.html。"""
    template = _env.get_template("index.html.j2")
    html = template.render(**snapshot)
    _OUT_DIR.mkdir(exist_ok=True)
    out_path = _OUT_DIR / "index.html"
    out_path.write_text(html, encoding="utf-8")
    return html
