"""Tests for scripts/code_index.py chunker + BM25/merge logic.

Deliberately does NOT load the embedding model or FAISS index build path —
only pure functions (AST/Markdown chunking, tokenizing, BM25 scoring).
Skips entirely if faiss/sentence-transformers are not installed, matching
the optional `codeindex` extra.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

faiss = pytest.importorskip("faiss", reason="codeindex extra not installed")
pytest.importorskip("sentence_transformers", reason="codeindex extra not installed")

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "code_index.py"

spec = importlib.util.spec_from_file_location("code_index", SCRIPT_PATH)
code_index = importlib.util.module_from_spec(spec)
sys.modules["code_index"] = code_index
spec.loader.exec_module(code_index)


PY_SAMPLE = '''"""Module docstring for sample."""


class Foo:
    """A sample class."""

    def bar(self, x, y=1):
        """Adds x and y."""
        return x + y


def top_level(a, b):
    """Top level function."""
    return a - b
'''

MD_SAMPLE = """# Title

Intro paragraph.

## Section One

Body of section one.

## Section Two

Body of section two, with more text.
"""


def test_chunk_python_file_produces_module_class_method_function():
    chunks = code_index.chunk_python_file("pkg/sample.py", PY_SAMPLE)
    names = {c.name for c in chunks}
    assert "sample" in names  # module chunk
    assert "Foo" in names  # class chunk
    assert "Foo.bar" in names  # method chunk, qualified
    assert "top_level" in names  # function chunk
    method_chunk = next(c for c in chunks if c.name == "Foo.bar")
    assert "def bar(self, x, y" in method_chunk.text
    assert "Adds x and y" in method_chunk.text


def test_chunk_python_file_handles_syntax_error():
    assert code_index.chunk_python_file("bad.py", "def broken(:\n") == []


def test_chunk_markdown_file_splits_on_headings():
    chunks = code_index.chunk_markdown_file("docs/x.md", MD_SAMPLE)
    names = [c.name for c in chunks]
    assert "Section One" in names
    assert "Section Two" in names
    sec1 = next(c for c in chunks if c.name == "Section One")
    assert "Body of section one" in sec1.text
    assert "Body of section two" not in sec1.text


def test_chunk_markdown_file_no_headings_yields_one_chunk():
    chunks = code_index.chunk_markdown_file("docs/plain.md", "just some text\nmore text\n")
    assert len(chunks) == 1
    assert "just some text" in chunks[0].text


def test_tokenize_splits_snake_case_and_drops_stopwords():
    toks = code_index.tokenize("How does widget_factory work for the gadget builder")
    assert "widget_factory" in toks
    assert "widget" in toks
    assert "factory" in toks
    assert "the" not in toks
    assert "how" not in toks


def test_bm25_scores_higher_for_matching_document():
    docs_tokens = [
        code_index.tokenize("widget_factory gadget builder throughput tuning"),
        code_index.tokenize("export xlsx spreadsheet writer for leads"),
    ]
    bm25 = code_index.BM25(docs_tokens)
    q = code_index.tokenize("gadget builder widget factory")
    s0 = bm25.score(q, 0)
    s1 = bm25.score(q, 1)
    assert s0 > s1


def test_bm25_empty_corpus_is_safe():
    bm25 = code_index.BM25([])
    assert bm25.score(["anything"], 0) == 0.0
