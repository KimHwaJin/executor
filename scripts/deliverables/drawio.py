"""Minimal builder for uncompressed draw.io (mxGraph) files.

Only what the generated diagrams need: boxes, text, table-like swimlanes
and edges. Files stay plain XML so Git can diff them.
"""

from pathlib import Path
from xml.etree import ElementTree as ET

# One palette for every diagram: (fill, stroke) per kind of component.
PALETTE = {
    "service": ("#dae8fc", "#6c8ebf"),
    "store": ("#d5e8d4", "#82b366"),
    "caller": ("#ffe6cc", "#d79b00"),
    "platform": ("#f5f5f5", "#666666"),
    "runtime": ("#e1d5e7", "#9673a6"),
    "warn": ("#f8cecc", "#b85450"),
    "note": ("#fff2cc", "#d6b656"),
    "plain": ("#ffffff", "#333333"),
}
FONT = "fontFamily=Malgun Gothic;fontSize=12;"


class Diagram:
    def __init__(
        self, name: str, width: int = 1600, height: int = 1000
    ) -> None:
        self._file = ET.Element(
            "mxfile", {"host": "app.diagrams.net", "compressed": "false"}
        )
        page = ET.SubElement(
            self._file, "diagram", {"name": name, "id": "page-1"}
        )
        model = ET.SubElement(
            page,
            "mxGraphModel",
            {
                "dx": "1200",
                "dy": "800",
                "grid": "1",
                "gridSize": "10",
                "guides": "1",
                "tooltips": "1",
                "connect": "1",
                "arrows": "1",
                "fold": "1",
                "page": "1",
                "pageScale": "1",
                "pageWidth": str(width),
                "pageHeight": str(height),
                "math": "0",
                "shadow": "0",
            },
        )
        self._root = ET.SubElement(model, "root")
        ET.SubElement(self._root, "mxCell", {"id": "0"})
        ET.SubElement(self._root, "mxCell", {"id": "1", "parent": "0"})
        self._count = 1

    def _cell(
        self, attributes: dict[str, str], geometry: dict[str, str]
    ) -> tuple[str, ET.Element]:
        self._count += 1
        identifier = f"c{self._count}"
        cell = ET.SubElement(
            self._root, "mxCell", {"id": identifier, **attributes}
        )
        shape = ET.SubElement(
            cell, "mxGeometry", {**geometry, "as": "geometry"}
        )
        return identifier, shape

    def _vertex(
        self, label: str, style: str, parent: str, **box: float
    ) -> str:
        return self._cell(
            {"value": label, "vertex": "1", "parent": parent, "style": style},
            {key: str(value) for key, value in box.items()},
        )[0]

    def node(
        self,
        label: str,
        x: float,
        y: float,
        w: float,
        h: float,
        kind: str = "plain",
        style: str = "rounded=1;",
        parent: str = "1",
    ) -> str:
        fill, stroke = PALETTE[kind]
        return self._vertex(
            label,
            f"whiteSpace=wrap;html=1;{FONT}fillColor={fill};strokeColor={stroke};{style}",
            parent,
            x=x,
            y=y,
            width=w,
            height=h,
        )

    def text(
        self,
        label: str,
        x: float,
        y: float,
        w: float,
        h: float,
        style: str = "align=left;verticalAlign=top;",
    ) -> str:
        return self._vertex(
            label,
            f"text;html=1;whiteSpace=wrap;strokeColor=none;fillColor=none;{FONT}{style}",
            "1",
            x=x,
            y=y,
            width=w,
            height=h,
        )

    def table(
        self,
        title: str,
        rows: list[str],
        x: float,
        y: float,
        w: float,
        kind: str = "plain",
        row_h: int = 18,
    ) -> list[str]:
        """An entity box: a header and one text row per item. Returns the row ids."""
        fill, stroke = PALETTE[kind]
        head = 26
        box = self._vertex(
            title,
            f"swimlane;fontStyle=1;childLayout=stackLayout;horizontal=1;startSize={head};"
            "horizontalStack=0;resizeParent=1;resizeParentMax=0;collapsible=0;"
            f"marginBottom=0;html=1;{FONT}fillColor={fill};strokeColor={stroke};"
            "swimlaneFillColor=#ffffff;",
            "1",
            x=x,
            y=y,
            width=w,
            height=head + row_h * len(rows),
        )
        return [
            self._vertex(
                row,
                "text;html=1;strokeColor=none;fillColor=none;align=left;"
                "verticalAlign=middle;spacingLeft=6;overflow=hidden;"
                "portConstraint=eastwest;fontFamily=Malgun Gothic;fontSize=11;",
                box,
                y=head + row_h * index,
                width=w,
                height=row_h,
            )
            for index, row in enumerate(rows)
        ]

    def edge(
        self,
        source: str,
        target: str,
        label: str = "",
        style: str = "",
        points: list[tuple[float, float]] | None = None,
    ) -> None:
        _, geometry = self._cell(
            {
                "value": label,
                "edge": "1",
                "parent": "1",
                "source": source,
                "target": target,
                "style": f"edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;{FONT}fontSize=11;"
                f"labelBackgroundColor=#ffffff;endArrow=block;endFill=1;{style}",
            },
            {"relative": "1"},
        )
        if points:
            array = ET.SubElement(geometry, "Array", {"as": "points"})
            for px, py in points:
                ET.SubElement(array, "mxPoint", {"x": str(px), "y": str(py)})

    def save(self, path: Path) -> None:
        ET.indent(self._file)
        path.write_text(
            ET.tostring(self._file, encoding="unicode") + "\n",
            encoding="utf-8",
        )
