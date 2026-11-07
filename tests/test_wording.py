"""Wording rules for everything a data manager or operator sees (messages, flags, the Upload Monitor, logs).

Every string in the source is checked except docstrings and the few that aren't words for people:
HTTP headers, the page's style and script, the state database's SQL and NetworkManager's files.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "hyperwheel"

NOT_FOR_PEOPLE = (
    re.compile(r"^(text|application|multipart)/[\w.+-]+;"),  # Content-Type
    re.compile(r"^default-src "),  # Content-Security-Policy
    re.compile(r"^\s*<!doctype html>", re.I),  # the page, with its style
    re.compile(r'^\s*"use strict";'),  # the page's script
    re.compile(r"^\s*(CREATE|ALTER) TABLE ", re.M),  # the state database's schema
    re.compile(r"^;$"),  # splitting that schema into statements
    re.compile(r"^\[connection\]\n"),  # NetworkManager connection files
)


def _docstrings(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def strings():
    """(file:line, text) for each string in the source that people may see."""
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        skip = _docstrings(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
                text = node.value
                if not any(p.search(text) for p in NOT_FOR_PEOPLE):
                    yield f"{path.relative_to(SRC)}:{node.lineno}", text


def test_no_semicolons_in_anything_people_see():
    found = [f"{where}: {text!r}" for where, text in strings() if ";" in text]
    assert not found, "Use a full stop instead:\n" + "\n".join(found)


def test_people_are_sent_to_their_hyperwheel_admin():
    words = re.compile(r"\bsupport\b|Hyperwheel team|the maintainer", re.I)
    found = [f"{where}: {text!r}" for where, text in strings() if words.search(text)]
    assert not found, "Say \"your Hyperwheel admin\" instead:\n" + "\n".join(found)


def test_the_check_sees_the_strings_it_should():
    seen = dict(strings())
    assert any("Hyperwheel admin" in t for t in seen.values())
    assert not any(t.startswith("<!doctype html>") or "CREATE TABLE" in t for t in seen.values())
