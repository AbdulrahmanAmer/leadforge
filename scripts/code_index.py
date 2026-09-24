#!/usr/bin/env python3
"""
Local semantic + keyword hybrid search index for the LeadForge repo.

Indexes:
  - src/**/*.py and tests/**/*.py as AST chunks (module/class/function/method)
  - Markdown in docs/, skills/, icm/, plus AGENTS.md, README.md, CHANGELOG.md,
    CLAUDE.md, .claude/POSITION.md, chunked by heading.

Storage: .vector_index/ (FAISS IndexFlatIP + chunks.jsonl + manifest.json).

Subcommands:
  build [--full]        (re)build the index, incrementally by default
  query "<text>" [-k N] [--kind code|docs|all] [--json]
  eval                   run scripts/code_index_eval.json, print hit@1/hit@5
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_DIR = REPO_ROOT / ".vector_index"
MODEL_NAME = "all-MiniLM-L6-v2"
MAX_BODY_LINES = 40
MD_CHUNK_MAX_CHARS = 2000

CODE_GLOBS = ["src/**/*.py", "tests/**/*.py"]
MD_DIRS = ["docs", "skills", "icm"]
MD_ROOT_FILES = [
    "AGENTS.md", "README.md", "CHANGELOG.md", "CLAUDE.md",
    ".claude/POSITION.md",
]

STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "in", "on", "for", "and", "or", "as", "at", "by", "with",
    "that", "this", "it", "from", "do", "does", "did", "how", "what",
    "where", "which", "who", "we", "our", "into", "not", "if", "when",
    "before", "after", "actually", "get", "gets",
}


@dataclass
class Chunk:
    id: str
    kind: str  # "code" or "docs"
    path: str
    start_line: int
    end_line: int
    name: str  # qualified name or heading
    text: str  # embedding text


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


# --------------------------------------------------------------------------
# Python AST chunking
# --------------------------------------------------------------------------

def _sig_of(node) -> str:
    try:
        args = []
        a = node.args
        for arg in a.posonlyargs + a.args:
            args.append(arg.arg)
        if a.vararg:
            args.append("*" + a.vararg.arg)
        for arg in a.kwonlyargs:
            args.append(arg.arg)
        if a.kwarg:
            args.append("**" + a.kwarg.arg)
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({', '.join(args)})"
    except Exception:
        return f"def {node.name}(...)"


def chunk_python_file(rel_path: str, text: str) -> list[Chunk]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines()
    chunks: list[Chunk] = []

    mod_doc = ast.get_docstring(tree)
    chunks.append(Chunk(
        id=f"{rel_path}:module:1",
        kind="code",
        path=rel_path,
        start_line=1,
        end_line=min(len(lines), MAX_BODY_LINES) or 1,
        name=Path(rel_path).stem,
        text="\n".join(filter(None, [
            rel_path,
            "module " + Path(rel_path).stem,
            mod_doc or "",
            "\n".join(lines[:MAX_BODY_LINES]),
        ])),
    ))

    def walk(node, parent_class: str | None):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                doc = ast.get_docstring(child)
                start, end = child.lineno, child.end_lineno or child.lineno
                body_lines = lines[start - 1:min(end, start - 1 + MAX_BODY_LINES)]
                chunks.append(Chunk(
                    id=f"{rel_path}:class:{child.name}:{start}",
                    kind="code",
                    path=rel_path,
                    start_line=start,
                    end_line=end,
                    name=child.name,
                    text="\n".join(filter(None, [
                        rel_path,
                        f"class {child.name}",
                        doc or "",
                        "\n".join(body_lines),
                    ])),
                ))
                walk(child, child.name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(child)
                sig = _sig_of(child)
                start, end = child.lineno, child.end_lineno or child.lineno
                body_lines = lines[start - 1:min(end, start - 1 + MAX_BODY_LINES)]
                qualname = f"{parent_class}.{child.name}" if parent_class else child.name
                chunk_type = "method" if parent_class else "function"
                chunks.append(Chunk(
                    id=f"{rel_path}:{chunk_type}:{qualname}:{start}",
                    kind="code",
                    path=rel_path,
                    start_line=start,
                    end_line=end,
                    name=qualname,
                    text="\n".join(filter(None, [
                        rel_path,
                        qualname,
                        sig,
                        doc or "",
                        "\n".join(body_lines),
                    ])),
                ))
                # do not descend into nested functions for extra chunks beyond one level
                walk(child, parent_class)
            else:
                walk(child, parent_class)

    walk(tree, None)
    return chunks


# --------------------------------------------------------------------------
# Markdown chunking (by heading)
# --------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,2})\s+(.*)$")


def chunk_markdown_file(rel_path: str, text: str) -> list[Chunk]:
    lines = text.splitlines()
    chunks: list[Chunk] = []
    cur_heading = Path(rel_path).stem
    cur_start = 1
    buf: list[str] = []

    def flush(end_line: int):
        if not buf:
            return
        body = "\n".join(buf).strip()
        if not body:
            return
        body = body[:MD_CHUNK_MAX_CHARS]
        chunks.append(Chunk(
            id=f"{rel_path}:heading:{cur_heading}:{cur_start}",
            kind="docs",
            path=rel_path,
            start_line=cur_start,
            end_line=end_line,
            name=cur_heading,
            text=f"{rel_path}\n{cur_heading}\n{body}",
        ))

    for i, line in enumerate(lines, start=1):
        m = _HEADING_RE.match(line)
        if m:
            flush(i - 1)
            cur_heading = m.group(2).strip()
            cur_start = i
            buf = []
        else:
            buf.append(line)
    flush(len(lines))
    if not chunks:
        # No headings found at all — index whole file as one chunk
        body = text[:MD_CHUNK_MAX_CHARS]
        chunks.append(Chunk(
            id=f"{rel_path}:heading:{cur_heading}:1",
            kind="docs",
            path=rel_path,
            start_line=1,
            end_line=len(lines),
            name=cur_heading,
            text=f"{rel_path}\n{cur_heading}\n{body}",
        ))
    return chunks


# --------------------------------------------------------------------------
# File discovery
# --------------------------------------------------------------------------

def discover_files() -> list[Path]:
    files: list[Path] = []
    for pat in CODE_GLOBS:
        files.extend(sorted(REPO_ROOT.glob(pat)))
    for d in MD_DIRS:
        p = REPO_ROOT / d
        if p.exists():
            files.extend(sorted(p.rglob("*.md")))
    for rel in MD_ROOT_FILES:
        p = REPO_ROOT / rel
        if p.exists():
            files.append(p)
    # de-dup, keep only files under repo root
    seen = set()
    out = []
    for f in files:
        rf = f.resolve()
        if rf not in seen and rf.is_file():
            seen.add(rf)
            out.append(f)
    return out


def chunk_file(path: Path) -> list[Chunk]:
    rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    if path.suffix == ".py":
        return chunk_python_file(rel, text)
    if path.suffix == ".md":
        return chunk_markdown_file(rel, text)
    return []


# --------------------------------------------------------------------------
# BM25-ish keyword scoring
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


def tokenize(text: str) -> list[str]:
    toks = []
    for raw in _TOKEN_RE.findall(text.lower()):
        if raw in STOPWORDS or len(raw) <= 1:
            continue
        toks.append(raw)
        # split snake_case / identifiers further for keyword overlap
        parts = raw.split("_")
        if len(parts) > 1:
            toks.extend(p for p in parts if p and p not in STOPWORDS and len(p) > 1)
    return toks


class BM25:
    """Minimal BM25 over the chunk corpus."""

    def __init__(self, docs_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs_tokens = docs_tokens
        self.N = len(docs_tokens)
        self.doc_len = [len(d) for d in docs_tokens]
        self.avgdl = (sum(self.doc_len) / self.N) if self.N else 0.0
        self.df: Counter = Counter()
        self.tf: list[Counter] = []
        for toks in docs_tokens:
            c = Counter(toks)
            self.tf.append(c)
            for term in c:
                self.df[term] += 1
        self.idf: dict[str, float] = {}
        for term, df in self.df.items():
            self.idf[term] = math.log(1 + (self.N - df + 0.5) / (df + 0.5))

    def score(self, query_tokens: list[str], doc_idx: int) -> float:
        if self.N == 0:
            return 0.0
        score = 0.0
        dl = self.doc_len[doc_idx]
        tf = self.tf[doc_idx]
        for term in query_tokens:
            if term not in tf:
                continue
            idf = self.idf.get(term, 0.0)
            f = tf[term]
            denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
            score += idf * (f * (self.k1 + 1)) / (denom or 1)
        return score


# --------------------------------------------------------------------------
# Index build / load
# --------------------------------------------------------------------------

def manifest_path() -> Path:
    return INDEX_DIR / "manifest.json"


def chunks_path() -> Path:
    return INDEX_DIR / "chunks.jsonl"


def faiss_path() -> Path:
    return INDEX_DIR / "index.faiss"


def load_manifest() -> dict | None:
    if manifest_path().exists():
        return json.loads(manifest_path().read_text(encoding="utf-8"))
    return None


def cmd_build(args: argparse.Namespace) -> int:
    import faiss
    import numpy as np
    from sentence_transformers import SentenceTransformer

    t0 = time.time()
    INDEX_DIR.mkdir(parents=True, exist_ok=True)

    old_manifest = load_manifest() if not args.full else None
    old_hashes: dict[str, str] = (old_manifest or {}).get("file_hashes", {})
    old_model = (old_manifest or {}).get("model")

    files = discover_files()
    new_hashes: dict[str, str] = {}
    changed_files: list[Path] = []
    unchanged_rel: set[str] = set()

    for f in files:
        rel = str(f.relative_to(REPO_ROOT)).replace("\\", "/")
        h = sha256_file(f)
        new_hashes[rel] = h
        if not args.full and old_model == MODEL_NAME and old_hashes.get(rel) == h:
            unchanged_rel.add(rel)
        else:
            changed_files.append(f)

    # Load old chunks for unchanged files (reuse embeddings by rebuilding text
    # is cheap; but to truly skip re-embedding we keep old chunk records+vectors)
    old_chunks: list[dict] = []
    old_vecs = None
    if unchanged_rel and chunks_path().exists() and faiss_path().exists():
        old_chunks = [json.loads(ln) for ln in chunks_path().read_text(encoding="utf-8").splitlines() if ln.strip()]
        idx = faiss.read_index(str(faiss_path()))
        old_vecs = idx.reconstruct_n(0, idx.ntotal)

    kept_chunks: list[dict] = []
    kept_vecs: list[Any] = []
    if old_chunks is not None and old_vecs is not None:
        for i, c in enumerate(old_chunks):
            if c["path"] in unchanged_rel:
                kept_chunks.append(c)
                kept_vecs.append(old_vecs[i])

    new_chunk_objs: list[Chunk] = []
    for f in changed_files:
        new_chunk_objs.extend(chunk_file(f))

    model = SentenceTransformer(MODEL_NAME)
    if new_chunk_objs:
        texts = [c.text for c in new_chunk_objs]
        new_vecs = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
    else:
        new_vecs = np.zeros((0, 384), dtype="float32")

    all_chunks = kept_chunks + [
        {
            "id": c.id, "kind": c.kind, "path": c.path,
            "start_line": c.start_line, "end_line": c.end_line,
            "name": c.name, "text": c.text,
        }
        for c in new_chunk_objs
    ]
    if kept_vecs:
        all_vecs = np.vstack([np.array(kept_vecs, dtype="float32"), np.asarray(new_vecs, dtype="float32")]) if len(new_vecs) else np.array(kept_vecs, dtype="float32")
    else:
        all_vecs = np.asarray(new_vecs, dtype="float32")

    dim = all_vecs.shape[1] if all_vecs.size else model.get_sentence_embedding_dimension()
    index = faiss.IndexFlatIP(dim)
    if all_vecs.size:
        index.add(all_vecs)
    faiss.write_index(index, str(faiss_path()))

    with chunks_path().open("w", encoding="utf-8") as fh:
        for c in all_chunks:
            fh.write(json.dumps(c) + "\n")

    manifest = {
        "model": MODEL_NAME,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "chunk_count": len(all_chunks),
        "file_hashes": new_hashes,
    }
    manifest_path().write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    elapsed = time.time() - t0
    summary = {
        "files_total": len(files),
        "files_reembedded": len(changed_files),
        "files_unchanged": len(unchanged_rel),
        "chunk_count": len(all_chunks),
        "elapsed_s": round(elapsed, 2),
        "full": bool(args.full),
    }
    print(json.dumps(summary))
    return 0


# --------------------------------------------------------------------------
# Query
# --------------------------------------------------------------------------

def _load_index_for_query():
    import faiss
    if not (manifest_path().exists() and chunks_path().exists() and faiss_path().exists()):
        print("No index found. Run `build` first.", file=sys.stderr)
        sys.exit(1)
    chunks = [json.loads(ln) for ln in chunks_path().read_text(encoding="utf-8").splitlines() if ln.strip()]
    idx = faiss.read_index(str(faiss_path()))
    return chunks, idx


def hybrid_query(query: str, k: int, kind: str) -> list[dict]:
    import numpy as np
    from sentence_transformers import SentenceTransformer

    chunks, idx = _load_index_for_query()
    all_vecs = idx.reconstruct_n(0, idx.ntotal) if idx.ntotal else np.zeros((0, 384), dtype="float32")

    keep_idx = [i for i, c in enumerate(chunks) if kind == "all" or c["kind"] == kind]

    model = SentenceTransformer(MODEL_NAME)
    qvec = model.encode([query], normalize_embeddings=True)[0]

    cos_scores = {}
    for i in keep_idx:
        cos_scores[i] = float(np.dot(all_vecs[i], qvec))

    docs_tokens = [tokenize(chunks[i]["text"]) for i in keep_idx]
    bm25 = BM25(docs_tokens)
    q_tokens = tokenize(query)
    bm25_scores_raw = {}
    for pos, i in enumerate(keep_idx):
        bm25_scores_raw[i] = bm25.score(q_tokens, pos)

    max_bm25 = max(bm25_scores_raw.values()) if bm25_scores_raw else 1.0
    max_bm25 = max_bm25 or 1.0

    # Weighting: 0.45 * cosine (semantic similarity, roughly in [0,1] after
    # normalize_embeddings) + 0.55 * normalized BM25 (keyword/identifier
    # overlap). MiniLM alone is weak on identifiers like `auto_label` or
    # `PolitenessCfg`, so keyword overlap is weighted slightly higher; a
    # small path-token bonus additionally rewards chunks whose file/module
    # name literally matches a query word (e.g. "config" -> config.py).
    W_COS, W_BM25, W_PATH = 0.45, 0.55, 0.08
    q_tokens_set = set(q_tokens)
    results = []
    for i in keep_idx:
        cos = cos_scores[i]
        bm25n = bm25_scores_raw[i] / max_bm25
        path = chunks[i]["path"]
        path_tokens = set(tokenize(path))
        path_bonus = W_PATH if (path_tokens & q_tokens_set) else 0.0
        # Implementation source (src/) slightly outranks its own tests when a
        # query describes behavior in implementation terms ("how does X
        # decide/apply/fetch"); tests reuse the same vocabulary in asserts
        # and would otherwise crowd out the real implementation.
        src_bonus = 0.04 if path.startswith("src/") else 0.0
        combined = W_COS * cos + W_BM25 * bm25n + path_bonus + src_bonus
        results.append((combined, i))
    results.sort(key=lambda x: x[0], reverse=True)

    out = []
    for score, i in results[:k]:
        c = chunks[i]
        out.append({
            "score": round(score, 4),
            "path": c["path"],
            "start_line": c["start_line"],
            "name": c["name"],
            "kind": c["kind"],
        })
    return out


def cmd_query(args: argparse.Namespace) -> int:
    results = hybrid_query(args.text, args.k, args.kind)
    if args.json:
        print(json.dumps(results, indent=2))
        return 0
    for rank, r in enumerate(results, start=1):
        print(f"{rank}. score={r['score']:.4f} {r['path']}:{r['start_line']} [{r['kind']}] {r['name']}")
    return 0


# --------------------------------------------------------------------------
# Eval
# --------------------------------------------------------------------------

def cmd_eval(args: argparse.Namespace) -> int:
    eval_path = REPO_ROOT / "scripts" / "code_index_eval.json"
    questions = json.loads(eval_path.read_text(encoding="utf-8"))
    hit1 = 0
    hit5 = 0
    misses = []
    for q in questions:
        results = hybrid_query(q["q"], 5, "all")
        paths5 = [r["path"] for r in results]
        top1 = paths5[0] if paths5 else None
        if top1 == q["path"]:
            hit1 += 1
        if q["path"] in paths5:
            hit5 += 1
        else:
            misses.append({"q": q["q"], "expected": q["path"], "got": paths5})
    n = len(questions)
    print(json.dumps({"n": n, "hit_at_1": hit1, "hit_at_5": hit5}))
    for m in misses:
        print("MISS:", json.dumps(m))
    return 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    # Windows consoles default to cp1252; headings contain em dashes and other non-ASCII text.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build")
    b.add_argument("--full", action="store_true")
    b.set_defaults(func=cmd_build)

    q = sub.add_parser("query")
    q.add_argument("text")
    q.add_argument("-k", type=int, default=8)
    q.add_argument("--kind", choices=["code", "docs", "all"], default="all")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_query)

    e = sub.add_parser("eval")
    e.set_defaults(func=cmd_eval)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
