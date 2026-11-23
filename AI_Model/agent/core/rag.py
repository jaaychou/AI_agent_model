"""Small, fully local document index backed by SQLite FTS5."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import zipfile
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree


TEXT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".java", ".c",
    ".cpp", ".h", ".go", ".rs", ".sql", ".log", ".ini", ".xml",
}
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | {".pdf", ".docx"}


def database_path(workspace: str | Path, data_dir: str | Path) -> Path:
    ws = str(Path(workspace).resolve()).casefold()
    key = hashlib.sha256(ws.encode("utf-8")).hexdigest()[:20]
    return Path(data_dir) / "knowledge" / f"{key}.sqlite3"


def _read_document(path: Path) -> list[tuple[str, str]]:
    """Return (page/section, text) pairs. Parsers are local and dependency-light."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("读取 PDF 需要 pypdf；请在 Agent 环境安装 pypdf。") from exc
        reader = PdfReader(str(path))
        return [(str(i + 1), page.extract_text() or "") for i, page in enumerate(reader.pages)]
    if ext == ".docx":
        with zipfile.ZipFile(path) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs = []
        for paragraph in root.findall(".//w:p", ns):
            value = "".join(node.text or "" for node in paragraph.findall(".//w:t", ns)).strip()
            if value:
                paragraphs.append(value)
        return [("正文", "\n".join(paragraphs))]
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            return [("正文", raw.decode(encoding))]
        except UnicodeError:
            continue
    return [("正文", raw.decode("utf-8", errors="replace"))]


def _split_chunks(text: str, max_chars: int = 1100, overlap: int = 160) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        while len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            cut = paragraph.rfind("。", 0, max_chars)
            if cut < max_chars // 2:
                cut = max_chars
            chunks.append(paragraph[:cut + 1].strip())
            paragraph = paragraph[max(0, cut + 1 - overlap):].strip()
        if current and len(current) + len(paragraph) + 1 > max_chars:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            current = (tail + "\n" + paragraph).strip()
        else:
            current = (current + "\n" + paragraph).strip()
    if current:
        chunks.append(current)
    return chunks


class LocalKnowledgeBase:
    """SQLite full-text index; optionally accept sources outside its root."""

    def __init__(self, workspace: str | Path, data_dir: str | Path, allow_external: bool = False):
        self.workspace = Path(workspace).resolve()
        self.allow_external = allow_external
        self.path = database_path(self.workspace, data_dir)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _init_db(self):
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY, source TEXT UNIQUE, modified_ns INTEGER, size INTEGER)")
            db.execute("CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL, page TEXT, content TEXT NOT NULL, FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE)")
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(content, source UNINDEXED, page UNINDEXED, tokenize='unicode61')")

    def _workspace_path(self, raw: str | Path) -> Path:
        path = Path(raw).expanduser().resolve()
        try:
            path.relative_to(self.workspace)
        except ValueError as exc:
            if not self.allow_external:
                raise ValueError(f"知识库文件必须位于当前工作空间内：{self.workspace}") from exc
        return path

    def add_file(self, raw_path: str | Path) -> int:
        path = self._workspace_path(raw_path)
        if not path.is_file():
            raise ValueError(f"不是有效文件：{path}")
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"暂不支持 {path.suffix or '无扩展名'}；支持 PDF、DOCX、TXT、Markdown 和常见代码/文本格式。")
        stat = path.stat()
        try:
            source = path.relative_to(self.workspace).as_posix()
        except ValueError:
            # Absolute source keys make shared indexes stable across workspace changes.
            source = str(path)
        pages = _read_document(path)
        chunks = [(page, chunk) for page, content in pages for chunk in _split_chunks(content)]
        if not chunks:
            raise ValueError(f"没有提取到可索引文本：{source}")
        with self._connect() as db:
            old = db.execute("SELECT id FROM documents WHERE source=?", (source,)).fetchone()
            if old:
                rows = db.execute("SELECT id FROM chunks WHERE document_id=?", (old["id"],)).fetchall()
                db.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(row["id"],) for row in rows])
                db.execute("DELETE FROM chunks WHERE document_id=?", (old["id"],))
                db.execute("UPDATE documents SET modified_ns=?, size=? WHERE id=?", (stat.st_mtime_ns, stat.st_size, old["id"]))
                doc_id = old["id"]
            else:
                cur = db.execute("INSERT INTO documents(source, modified_ns, size) VALUES(?,?,?)", (source, stat.st_mtime_ns, stat.st_size))
                doc_id = cur.lastrowid
            for page, content in chunks:
                cur = db.execute("INSERT INTO chunks(document_id, page, content) VALUES(?,?,?)", (doc_id, page, content))
                db.execute("INSERT INTO chunks_fts(rowid, content, source, page) VALUES(?,?,?,?)", (cur.lastrowid, content, source, page))
        return len(chunks)

    def list_documents(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT d.source, d.size, COUNT(c.id) AS chunks FROM documents d LEFT JOIN chunks c ON c.document_id=d.id GROUP BY d.id ORDER BY d.source COLLATE NOCASE").fetchall()
        return [dict(row) for row in rows]

    def remove_file(self, source: str):
        with self._connect() as db:
            row = db.execute("SELECT id FROM documents WHERE source=?", (source,)).fetchone()
            if not row:
                return False
            ids = db.execute("SELECT id FROM chunks WHERE document_id=?", (row["id"],)).fetchall()
            db.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(r["id"],) for r in ids])
            db.execute("DELETE FROM chunks WHERE document_id=?", (row["id"],))
            db.execute("DELETE FROM documents WHERE id=?", (row["id"],))
        return True

    @staticmethod
    def _terms(query: str) -> list[str]:
        latin = re.findall(r"[A-Za-z0-9_]+", query)
        cjk = "".join(re.findall(r"[\u3400-\u9fff]", query))
        terms = latin + [cjk[i:i + 2] for i in range(max(1, len(cjk) - 1))] if cjk else latin
        return list(dict.fromkeys(term for term in terms if term))[:24]

    def search(self, query: str, limit: int = 4) -> list[dict]:
        terms = self._terms(query)
        if not terms:
            return []
        match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        with self._connect() as db:
            try:
                rows = db.execute("SELECT source, page, content, bm25(chunks_fts) AS score FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?", (match, limit)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            if not rows:
                # Chinese substring fallback for tokenizers that do not split a particular query well.
                candidates = db.execute("SELECT source, page, content FROM chunks_fts").fetchall()
                scored = []
                chars = [term for term in terms if len(term) == 2]
                for row in candidates:
                    score = sum(row["content"].count(term) for term in terms)
                    if score:
                        scored.append((score, row))
                scored.sort(key=lambda pair: pair[0], reverse=True)
                rows = [pair[1] for pair in scored[:limit]]
        return [dict(row) for row in rows]
