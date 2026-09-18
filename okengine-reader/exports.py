"""Portable Markdown and document conversion services."""
from __future__ import annotations

import re
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import Response

PDF_CSS = (
    "@page{size:A4;margin:1.8cm 1.7cm}"
    "html{font-family:'DejaVu Serif',serif;font-size:10.5pt;line-height:1.42}"
    "body{max-width:100%}"
    "h1{font-size:18pt;margin:0 0 .3em}h2{font-size:13.5pt;margin:1.1em 0 .3em}"
    "h3{font-size:11.5pt;margin:.9em 0 .2em}"
    "p,li{overflow-wrap:break-word;word-wrap:break-word}"
    "pre,code{white-space:pre-wrap;word-break:break-word;font-size:9pt}"
    "pre{background:#f5f5f5;padding:6px 8px;border-radius:4px}"
    "table{width:100%;table-layout:fixed;border-collapse:collapse;font-size:8.6pt;margin:.6em 0}"
    "th,td{border:1px solid #bbb;padding:3px 5px;vertical-align:top;"
    "overflow-wrap:break-word;word-break:break-word}"
    "th{background:#f0f0f0;text-align:left}"
    "img{max-width:100%}a{color:inherit;text-decoration:none}"
)
MIME_TYPES = {
    "md": "text/markdown; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


def download_response(
    request, fmt: str, path: str, *, mime_types: dict, resolve_page,
    split_frontmatter, clean_markdown_text, exports_enabled: bool, guard,
    semaphore, convert_document,
):
    """Build a guarded portable page download response."""
    if fmt not in mime_types:
        raise HTTPException(400, "fmt must be md|docx|pdf")
    page = resolve_page(path)
    raw = page.read_text(encoding="utf-8", errors="replace")
    frontmatter, _ = split_frontmatter(raw)
    title = str(frontmatter.get("title") or frontmatter.get("name") or Path(path).stem).strip()
    clean = clean_markdown_text(raw)
    if fmt == "md":
        data = clean.encode("utf-8")
    else:
        if not exports_enabled:
            raise HTTPException(
                403,
                "docx/pdf export is disabled on this deployment "
                "(use md, or set OKENGINE_READER_EXPORTS=1)",
            )
        release = guard(request, semaphore)
        try:
            data = convert_document(clean, fmt, title=title)
        finally:
            release()
    filename = f"{Path(path).name}.{fmt}"
    return Response(
        content=data,
        media_type=mime_types[fmt],
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


def clean_markdown(
    raw: str,
    title: str | None,
    *,
    split_frontmatter,
    resolve_embeds,
    uncode_wikilinks,
    delink,
    deref_local_links,
) -> str:
    frontmatter, body = split_frontmatter(raw)
    body = resolve_embeds(body)
    body = re.sub(r"```dataview(js)?\n.*?\n```", "", body, flags=re.DOTALL)
    body = uncode_wikilinks(body)
    body = delink(body)
    body = deref_local_links(body)
    resolved_title = str(
        title or frontmatter.get("title") or frontmatter.get("name") or ""
    ).strip()
    if resolved_title and not body.lstrip().startswith("# "):
        body = f"# {resolved_title}\n\n{body}"
    return body.strip() + "\n"


def convert(clean_md: str, fmt: str, title: str | None, *, subprocess_module, pdf_css: str) -> bytes:
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / "in.md"
        output = Path(temporary) / f"out.{fmt}"
        source.write_text(clean_md, encoding="utf-8")
        command = ["pandoc", str(source), "-f", "markdown+pipe_tables", "-o", str(output)]
        command += ["--metadata", f"title={(title or '').strip() or 'OKEngine page'}"]
        if fmt == "docx":
            command += ["--standalone"]
        if fmt == "pdf":
            command += ["--pdf-engine=weasyprint"]
            header = Path(temporary) / "style.html"
            header.write_text(f"<style>{pdf_css}</style>", encoding="utf-8")
            command += ["--standalone", "-H", str(header)]
        try:
            subprocess_module.run(
                command, check=True, capture_output=True, timeout=90, cwd=temporary
            )
        except FileNotFoundError as exc:
            raise HTTPException(503, "pandoc not installed") from exc
        except subprocess_module.CalledProcessError as exc:
            detail = exc.stderr.decode("utf-8", "replace")[:300]
            raise HTTPException(500, f"convert failed: {detail}") from exc
        return output.read_bytes()
