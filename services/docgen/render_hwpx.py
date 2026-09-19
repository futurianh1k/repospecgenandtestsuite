"""HWPX 렌더러 — 문서 IR을 HWPX(OWPML)로 출력한다.

공공 납품물은 HWP 계열을 요구하는 경우가 많다. python-hwpx로 OWPML을 직접
생성하며, 한글 없이도 구조 검증(ZIP 무결성, XML 적합성, mimetype)은 가능하다.

주의: 구조 검증이 통과해도 한글에서 정상 열람된다는 보장은 아니다.
실제 납품 전 한글 실물 검수가 필요하다. LibreOffice에는 HWPX 임포트
필터가 없어 이 환경에서는 교차 검증이 불가능하다.
"""

from __future__ import annotations

import io
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Block:
    """문서 IR의 최소 단위."""
    kind: str  # heading | para | table
    text: str = ""
    level: int = 1
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class DocumentIR:
    title: str
    doc_id: str
    profile: str
    blocks: list[Block] = field(default_factory=list)

    def heading(self, text: str, level: int = 1) -> "DocumentIR":
        self.blocks.append(Block("heading", text=text, level=level))
        return self

    def para(self, text: str) -> "DocumentIR":
        self.blocks.append(Block("para", text=text))
        return self

    def table(self, rows: list[list[str]]) -> "DocumentIR":
        self.blocks.append(Block("table", rows=rows))
        return self


class HwpxUnavailable(RuntimeError):
    pass


def render(ir: DocumentIR) -> bytes:
    try:
        from hwpx.document import HwpxDocument
        from hwpx.templates import blank_document_bytes
    except ImportError as exc:  # pragma: no cover
        raise HwpxUnavailable(
            "python-hwpx가 설치되지 않았다. pip install python-hwpx"
        ) from exc

    doc = HwpxDocument.open(io.BytesIO(blank_document_bytes()))
    doc.add_heading(ir.title, level=1)
    doc.add_paragraph(f"문서번호: {ir.doc_id}    프로파일: {ir.profile}")

    for block in ir.blocks:
        if block.kind == "heading":
            doc.add_heading(block.text, level=min(block.level + 1, 4))
        elif block.kind == "para":
            doc.add_paragraph(block.text)
        elif block.kind == "table":
            if not block.rows:
                continue
            cols = max(len(r) for r in block.rows)
            tbl = doc.add_table(len(block.rows), cols)
            for i, row in enumerate(block.rows):
                for j in range(cols):
                    tbl.cell(i, j).text = row[j] if j < len(row) else ""
    return doc.to_bytes()


def verify(data: bytes) -> dict[str, Any]:
    """구조 검증. 한글 열람 가능 여부는 확인하지 못한다."""
    report: dict[str, Any] = {"ok": True, "checks": {}, "caveat": (
        "구조 검증만 수행했다. 한글 실물 검수가 별도로 필요하다."
    )}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            report["checks"]["zip_integrity"] = z.testzip() is None
            names = z.namelist()
            report["checks"]["entry_count"] = len(names)
            mt = z.read("mimetype").decode() if "mimetype" in names else None
            report["checks"]["mimetype"] = mt
            report["checks"]["mimetype_ok"] = mt == "application/hwp+zip"

            xmls = [n for n in names if n.endswith(".xml")]
            bad = []
            for n in xmls:
                try:
                    ET.fromstring(z.read(n))
                except ET.ParseError as exc:
                    bad.append(f"{n}: {exc}")
            report["checks"]["xml_total"] = len(xmls)
            report["checks"]["xml_malformed"] = bad
    except zipfile.BadZipFile as exc:
        report["ok"] = False
        report["checks"]["zip_integrity"] = False
        report["error"] = str(exc)
        return report

    c = report["checks"]
    report["ok"] = bool(
        c.get("zip_integrity") and c.get("mimetype_ok") and not c.get("xml_malformed")
    )
    return report


def save(ir: DocumentIR, path: str | Path) -> dict[str, Any]:
    data = render(ir)
    Path(path).write_bytes(data)
    rep = verify(data)
    rep["path"] = str(path)
    rep["bytes"] = len(data)
    return rep
