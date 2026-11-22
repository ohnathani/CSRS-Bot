from dataclasses import dataclass
from html.parser import HTMLParser
import hashlib
import json
import re


_REQUIRED_HEADERS = {
    "course number",
    "section",
    "day",
    "time",
    "max size",
    "count",
    "wait list",
    "allowed",
    "add",
}
_ENLIST_CALL = re.compile(
    r"""^\s*javascript:\s*enlist\(\s*(['"])(.*?)\1\s*,\s*
        (['"])(.*?)\3\s*,\s*(['"])(.*?)\5\s*\)\s*;?\s*$""",
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)
_VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


@dataclass
class _Element:
    tag: str
    attributes: dict[str, str]
    parent: "_Element | None"
    children: list["_Element | str"]

    def descendants(self, tag: str | None = None):
        for child in self.children:
            if not isinstance(child, _Element):
                continue
            if tag is None or child.tag == tag:
                yield child
            yield from child.descendants(tag)

    def text_content(self) -> str:
        parts: list[str] = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag not in {"script", "style"}:
                parts.append(child.text_content())
        return " ".join("".join(parts).split())


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Element("#document", {}, None, [])
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = _Element(
            tag.lower(),
            {name.lower(): value or "" for name, value in attrs},
            self.stack[-1],
            [],
        )
        self.stack[-1].children.append(element)
        if element.tag not in _VOID_ELEMENTS:
            self.stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = _Element(
            tag.lower(),
            {name.lower(): value or "" for name, value in attrs},
            self.stack[-1],
            [],
        )
        self.stack[-1].children.append(element)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _descendants_in_table(element: _Element, tag: str):
    for child in element.children:
        if not isinstance(child, _Element) or child.tag == "table":
            continue
        if child.tag == tag:
            yield child
        yield from _descendants_in_table(child, tag)


def _ordered_cell_nodes(row: _Element) -> list[_Element]:
    cells: list[_Element] = []

    def visit(element: _Element) -> None:
        for child in element.children:
            if not isinstance(child, _Element) or child.tag == "table":
                continue
            if child.tag in {"td", "th"}:
                cells.append(child)
            else:
                visit(child)

    visit(row)
    return cells


def _table_category(table: _Element, rows: list[_Element]) -> str | None:
    for row in rows:
        cells = _ordered_cell_nodes(row)
        if not cells:
            continue
        course = cells[0].text_content().casefold()
        if course.startswith("nstp"):
            return "NSTP"
        if course.startswith("pe"):
            return "PE"

    ancestor = table.parent
    while ancestor is not None:
        if ancestor.tag == "div" and "panel" in ancestor.attributes.get("class", "").split():
            panel_text = ancestor.text_content().casefold()
            if "national service training program" in panel_text:
                return "NSTP"
            if "physical education" in panel_text:
                return "PE"
        ancestor = ancestor.parent
    return None


def parse_enlist_arguments(onclick: str) -> tuple[str, str, str] | None:
    """Parse only the literal three-string enlist call; never evaluate JavaScript."""
    match = _ENLIST_CALL.fullmatch(onclick)
    if match is None:
        return None
    return match.group(2), match.group(4), match.group(6)


def _parse_enlist_action(
    add_cell: _Element,
) -> tuple[tuple[str, str, str] | None, str | None]:
    matches: list[tuple[str, str, str]] = []
    labels: list[str] = []
    for element in add_cell.descendants():
        onclick = element.attributes.get("onclick", "")
        if "enlist" not in onclick.casefold():
            continue
        args = parse_enlist_arguments(onclick)
        if args is not None:
            matches.append(args)
            label = element.attributes.get("title") or element.attributes.get("alt")
            if label:
                labels.append(label)
    if len(matches) != 1:
        return None, None
    return matches[0], labels[0] if len(labels) == 1 else None


def _row_key(
    category: str,
    headers: list[str],
    cells: list[str],
    args: tuple[str, str, str] | None,
    occurrence: int,
) -> str:
    excluded = {"count", "wait list", "add"}
    stable_cells = [
        value
        for header, value in zip(headers, cells)
        if re.sub(r"\s+", " ", header.casefold()).strip() not in excluded
    ]
    identity = json.dumps(
        [category, stable_cells, occurrence, args is not None],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def parse_registration_tables(document_html: str) -> list[dict]:
    """Extract PE and NSTP table rows without executing page or source scripts."""
    parser = _DocumentParser()
    parser.feed(document_html)

    results: list[dict] = []
    occurrences: dict[tuple[str, tuple[str, ...]], int] = {}
    for table in parser.root.descendants("table"):
        rows = [
            row
            for row in _descendants_in_table(table, "tr")
            if _ordered_cell_nodes(row)
        ]
        if not rows:
            continue

        header_row_index = next(
            (
                index
                for index, row in enumerate(rows)
                if any(cell.tag == "th" for cell in _ordered_cell_nodes(row))
            ),
            None,
        )
        if header_row_index is None:
            continue

        header_nodes = _ordered_cell_nodes(rows[header_row_index])
        headers = [cell.text_content() for cell in header_nodes]
        normalized_headers = {
            re.sub(r"\s+", " ", header.casefold()).strip()
            for header in headers
        }
        if not _REQUIRED_HEADERS.issubset(normalized_headers):
            continue

        data_rows = rows[header_row_index + 1 :]
        category = _table_category(table, data_rows)
        if category is None:
            continue

        allowed_index = next(
            index
            for index, header in enumerate(headers)
            if re.sub(r"\s+", " ", header.casefold()).strip() == "allowed"
        )
        add_index = next(
            index
            for index, header in enumerate(headers)
            if re.sub(r"\s+", " ", header.casefold()).strip() == "add"
        )

        subjects: list[dict] = []
        for row in data_rows:
            cells_nodes = _ordered_cell_nodes(row)
            cells = [cell.text_content() for cell in cells_nodes]
            if len(cells) < len(headers):
                cells.extend([""] * (len(headers) - len(cells)))
            elif len(cells) > len(headers):
                cells = cells[: len(headers) - 1] + [
                    " ".join(cells[len(headers) - 1 :])
                ]
            args, action_label = (
                _parse_enlist_action(cells_nodes[add_index])
                if len(cells_nodes) > add_index
                else (None, None)
            )
            volatile_headers = {"count", "wait list", "add"}
            row_identity = tuple(
                value
                for index, value in enumerate(cells)
                if re.sub(r"\s+", " ", headers[index].casefold()).strip()
                not in volatile_headers
            )
            occurrence_key = (category, row_identity)
            occurrence = occurrences.get(occurrence_key, 0)
            occurrences[occurrence_key] = occurrence + 1
            key = _row_key(category, headers, cells, args, occurrence)

            denied = "not allowed" in cells[allowed_index].casefold()
            selectable = args is not None and not denied
            reason = None
            if denied:
                reason = "Not allowed"
            elif args is None:
                reason = "No live enlist control"

            subjects.append(
                {
                    "key": key,
                    "cells": cells,
                    "selectable": selectable,
                    "reason": reason,
                    "enlist_args": args,
                    "action_label": action_label,
                }
            )

        results.append(
            {
                "category": category,
                "headers": headers,
                "subjects": subjects,
            }
        )

    return results
