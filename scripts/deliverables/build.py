"""Build the Word and PDF deliverables from docs/deliverables/*.md.

Needs pandoc and LibreOffice (soffice) on PATH. Run from the repository root:
    uv run --with python-docx python scripts/deliverables/build.py
The two workbooks come from schema.py and programs.py; this script only
converts them to PDF.
"""

import re
import subprocess
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt

SOURCE = Path("docs/deliverables")
DIST = SOURCE / "dist"
REFERENCE = Path("dev_docs/api-reference")
FONT = "맑은 고딕"
# Submitted as a deck: the PDF comes from the .pptx, not from the Markdown.
# Other .pptx files are extra copies of a document and are not converted.
DECKS = {"08-운영자-매뉴얼"}
# Appended to a document as appendices: (document stem, [(title, file)]).
APPENDICES = {
    "03-프로그램-명세서": [
        ("부록 A. 실행 API", "01-executions.md"),
        ("부록 B. 관리 API", "02-admin.md"),
        ("부록 C. 헬스체크", "03-health.md"),
        ("부록 D. MCP", "04-mcp.md"),
        ("부록 E. 이벤트", "05-events.md"),
    ],
}


def run(*command: str, cwd: Path | None = None) -> None:
    subprocess.run(command, check=True, cwd=cwd, capture_output=True)


def markdown(source: Path) -> str:
    text = source.read_text(encoding="utf-8")
    # Parts kept only in the Markdown (the ERD as text) stay out of Word.
    text = re.sub(
        r"<!-- md-only:start -->.*?<!-- md-only:end -->", "", text, flags=re.S
    )
    for title, file in APPENDICES.get(source.stem, []):
        body = (REFERENCE / file).read_text(encoding="utf-8")
        # The reference's own title becomes the appendix title.
        text += f"\n\n# {title}\n\n" + body.split("\n", 1)[1]
    return text


def style(path: Path) -> None:
    """Landscape A4, Korean font, bordered tables and page-wide images."""
    document = Document(str(path))
    for section in document.sections:
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width, section.page_height = Mm(297), Mm(210)
        for side in (
            "left_margin",
            "right_margin",
            "top_margin",
            "bottom_margin",
        ):
            setattr(section, side, Mm(15))
    for item in document.styles:
        if (
            getattr(item, "font", None) is not None
            and "Code" not in item.name
            and "Verbatim" not in item.name
            and "Source" not in item.name
        ):
            item.font.name = FONT
            item.element.get_or_add_rPr().get_or_add_rFonts().set(
                qn("w:eastAsia"), FONT
            )
    document.styles["Normal"].font.size = Pt(10)
    for table in document.tables:
        borders = OxmlElement("w:tblBorders")
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
            line = OxmlElement(f"w:{edge}")
            for key, value in (
                ("val", "single"),
                ("sz", "4"),
                ("color", "999999"),
            ):
                line.set(qn(f"w:{key}"), value)
            borders.append(line)
        table._tbl.tblPr.append(borders)
        width = table._tbl.tblPr.find(qn("w:tblW"))
        width.set(qn("w:type"), "pct")
        width.set(qn("w:w"), "5000")
        for cell in table.rows[0].cells:
            for paragraph in cell.paragraphs:
                for text in paragraph.runs:
                    text.bold = True
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    for text in paragraph.runs:
                        text.font.size = Pt(9)
    for shape in document.inline_shapes:
        scale = min(Mm(267) / shape.width, Mm(160) / shape.height)
        shape.width, shape.height = (
            int(shape.width * scale),
            int(shape.height * scale),
        )
    document.save(str(path))


def main() -> None:
    DIST.mkdir(exist_ok=True)
    for source in sorted(SOURCE.glob("0*.md")):
        # A deliverable submitted as a deck or a workbook keeps its Markdown
        # for reference; a second PDF of the same name would overwrite the
        # submitted one.
        if source.stem in DECKS or (DIST / f"{source.stem}.xlsx").exists():
            continue
        target = DIST / f"{source.stem}.docx"
        subprocess.run(
            [
                "pandoc",
                "--from",
                "gfm",
                "--to",
                "docx",
                "--resource-path",
                str(SOURCE),
                "--output",
                str(target),
            ],
            input=markdown(source),
            text=True,
            check=True,
        )
        style(target)
        print(target)
    for office in sorted(
        [
            *DIST.glob("*.docx"),
            *DIST.glob("*.xlsx"),
            *(SOURCE / f"{stem}.pptx" for stem in DECKS),
        ]
    ):
        run(
            "soffice",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(DIST),
            str(office),
        )


if __name__ == "__main__":
    main()
