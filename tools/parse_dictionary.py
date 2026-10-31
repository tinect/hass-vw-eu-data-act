#!/usr/bin/env python3
"""One-off dev tool: parse the EU Data Act PDF data dictionary into JSON.

Extracts the table (Key | Data Point Name | Description | Measurement Unit |
Tech. Data Type | Data Cluster) from every page and writes a mapping keyed by
the stable data-point UUID:

    { "<key-uuid>": {"name", "description", "unit", "type", "cluster"}, ... }

The resulting custom_components/vw_eu_data_act/data_dictionary.json is committed
and shipped with the integration; pdfplumber is NOT a runtime dependency.

Usage:
    .venv/bin/python tools/parse_dictionary.py [PDF_PATH] [OUT_PATH]
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

import pdfplumber

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
HEADER_FIRST_CELL = "key"

# The V6.0 PDF stores UTF-8 text that was decoded as cp1252 somewhere upstream
# ("fÃ¼r", "20Â°C"). A run of a lead byte (Â..ô) followed by cp1252-rendered
# continuation bytes is re-encoded and decoded as UTF-8; runs that do not form
# valid UTF-8 are left untouched.
_CP1252_CONT = (
    "\u0080-\u00bf\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192"
    "\u02c6\u02dc\u2013\u2014\u2018-\u201e\u2020-\u2022\u2026\u2030"
    "\u2039\u203a\u20ac\u2122"
)
_MOJIBAKE_RE = re.compile(f"[\u00c2-\u00f4][{_CP1252_CONT}]+")
# Some continuation bytes (cp1252 has no glyph for 0x81/0x8d/0x8f/0x90/0x9d,
# and C1 controls were dropped) are missing entirely, leaving a bare "Ã".
# Mid-word it can only be "ß"; word-initial it is Ä/Ö/Ü, told apart by the
# German words that actually occur in the dictionary.
_BARE_A_TILDE_MIDWORD_RE = re.compile(r"(?<=[A-Za-zäöüÄÖÜß])Ã")
_BARE_A_TILDE_INITIAL = (("Ãber", "Über"), ("Ãff", "Öff"), ("Ãnder", "Änder"))
# A bare "Â" is a non-breaking space whose 0xA0 byte became a plain space.
_BARE_A_CIRC_RE = re.compile(r"Â(?=\s)")
# The PDF already carries U+FFFD where a degree sign was ("20�C", "(�F)").
_LOST_DEGREE_RE = re.compile(r"(?<=[\d(\s])\ufffd(?=[CF]\b)")
# Rich-text-editor markup leaked into some descriptions. Only real HTML tags
# are stripped; "<departureDate>" examples or "a < b > c" prose stay intact.
# PDF line wrapping can split a tag name ("<spa n style=...>").
_HTML_TAGS = {"p", "span", "div", "br", "b", "i", "u", "em", "strong", "ul", "ol", "li", "a", "font"}
_HTML_TAG_RE = re.compile(r"</?\s*([a-z](?:\s?[a-z])*)(?=[\s/>])[^<>]*>", re.I)


def _clean_key(cell: str | None) -> str:
    """UUIDs may wrap across lines; remove all whitespace."""
    return re.sub(r"\s+", "", cell or "")


def _clean_name(cell: str | None) -> str:
    """Join wrapped lines.

    Friendly names ("Remaining Charge Time Complete") legitimately contain
    spaces; code identifiers ("remaining_climate_time", dotted paths) do not.
    Heuristic: if any physical line already contains a space it is a friendly
    name -> join lines with a space; otherwise it is a code identifier whose
    line breaks are artifacts -> join with no separator.
    """
    if not cell:
        return ""
    lines = [ln.strip() for ln in _repair_encoding(cell).split("\n") if ln.strip()]
    sep = " " if any(" " in ln for ln in lines) else ""
    name = sep.join(lines)
    return re.sub(r"\s+", " ", name).strip()


def _repair_mojibake_run(match: re.Match[str]) -> str:
    run = match.group(0)
    try:
        return run.encode("cp1252").decode("utf-8")
    except UnicodeError:
        try:
            return run.encode("latin-1").decode("utf-8")
        except UnicodeError:
            return run


def _repair_encoding(text: str) -> str:
    """Undo UTF-8-read-as-cp1252 double encoding (see _MOJIBAKE_RE)."""
    text = _MOJIBAKE_RE.sub(_repair_mojibake_run, text)
    text = _BARE_A_TILDE_MIDWORD_RE.sub("ß", text)
    for broken, fixed in _BARE_A_TILDE_INITIAL:
        text = text.replace(broken, fixed)
    text = _BARE_A_CIRC_RE.sub("", text)
    text = _LOST_DEGREE_RE.sub("°", text)
    # "â" left over from an en dash whose trailing bytes were dropped.
    return text.replace(" â ", " – ")


def _strip_html_tag(match: re.Match[str]) -> str:
    name = re.sub(r"\s", "", match.group(1)).lower()
    return " " if name in _HTML_TAGS else match.group(0)


def _clean_text(cell: str | None) -> str:
    """Collapse wrapped multi-line text (descriptions) into one line.

    Some descriptions are raw rich-text-editor HTML; tags are stripped so only
    the visible text remains.
    """
    if not cell:
        return ""
    text = html.unescape(_HTML_TAG_RE.sub(_strip_html_tag, cell.replace("\n", " ")))
    text = _repair_encoding(text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_token(cell: str | None) -> str:
    """Single-token columns (unit / type). Treat placeholders as empty."""
    tok = _clean_text(cell)
    if tok in ("", "'-", "-", "'"):
        return ""
    return tok


def parse(pdf_path: Path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    dupes = 0
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    if not row or len(row) < 6:
                        continue
                    key = _clean_key(row[0])
                    if key.lower() == HEADER_FIRST_CELL:
                        continue
                    if not UUID_RE.match(key):
                        continue
                    entry = {
                        "name": _clean_name(row[1]),
                        "description": _clean_text(row[2]),
                        "unit": _clean_token(row[3]),
                        "type": _clean_token(row[4]).lower(),
                        "cluster": _clean_text(row[5]),
                    }
                    if key in out and out[key] != entry:
                        dupes += 1
                    out[key] = entry
    if dupes:
        print(f"  note: {dupes} duplicate keys with differing rows (last wins)")
    return out


def main() -> None:
    here = Path(__file__).resolve().parent.parent
    pdf_path = Path(sys.argv[1]) if len(sys.argv) > 1 else next(
        here.glob("*DataDictionary*.pdf")
    )
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else (
        here / "custom_components" / "vw_eu_data_act" / "data_dictionary.json"
    )
    print(f"Parsing {pdf_path.name} ...")
    mapping = parse(pdf_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(mapping, indent=1, ensure_ascii=False, sort_keys=True))
    print(f"Wrote {len(mapping)} data points -> {out_path}")

    # sanity check from the plan
    sample = "3c19831c-38b8-3dc5-9ead-bb333616d925"
    if sample in mapping:
        print(f"  check {sample}: {mapping[sample]}")
    else:
        print(f"  WARNING: expected key {sample} not found!")


if __name__ == "__main__":
    main()
