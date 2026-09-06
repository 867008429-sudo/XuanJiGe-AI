"""Local classics retrieval baseline for the Agentic RAG track (R2).

The index is intentionally small and dependency-free: the R1 corpus is only
about a thousand chunks, so an in-process BM25 pass is enough for the current
product stage. R3 can wrap this module as the `search_classics` agent tool and
add citation grounding on top of the returned chunk ids.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_CORPUS_DIR = ROOT / 'data' / 'classics'
MAX_RESULTS = 20

DOMAIN_TERMS = (
    '滴天髓', '滴天髓阐微', '子平真诠', '三命通会', '渊海子平', '穷通宝鉴', '神峰通考',
    '比肩', '劫财', '食神', '伤官', '正财', '偏财', '正官', '七杀', '正印', '偏印',
    '官杀', '财官', '印绶', '羊刃', '禄刃', '财星', '官星',
    '用神', '相神', '喜神', '忌神', '格局', '成败', '旺衰', '身旺', '身弱', '从格',
    '调候', '通关', '源流', '清浊', '寒暖', '燥湿', '顺逆',
    '天干', '地支', '月令', '藏干', '纳音', '冲', '刑', '合', '害', '合化',
    '夫妻', '子女', '父母', '女命', '男命', '甲木', '乙木', '丙火', '丁火',
    '戊土', '己土', '庚金', '辛金', '壬水', '癸水', '三春', '三夏', '三秋', '三冬',
)

TOKEN_RE = re.compile(r'[\u4e00-\u9fff]+|[A-Za-z0-9]+')


@dataclass(frozen=True)
class ClassicChunk:
    chunk_id: str
    book: str
    chapter_no: int
    chapter_title: str
    text: str
    source_file: str = ''

    @classmethod
    def from_row(cls, row):
        return cls(
            chunk_id=str(row['chunk_id']),
            book=str(row['book']),
            chapter_no=int(row.get('chapter_no') or 0),
            chapter_title=str(row.get('chapter_title') or ''),
            text=str(row.get('text') or ''),
            source_file=str(row.get('source_file') or ''),
        )

    def to_dict(self):
        return {
            'chunk_id': self.chunk_id,
            'book': self.book,
            'chapter_no': self.chapter_no,
            'chapter_title': self.chapter_title,
            'text': self.text,
            'source_file': self.source_file,
        }


@dataclass(frozen=True)
class SearchHit:
    chunk: ClassicChunk
    score: float
    matched_terms: tuple[str, ...]
    excerpt: str

    def to_dict(self):
        row = self.chunk.to_dict()
        row.update({
            'score': round(self.score, 4),
            'matched_terms': list(self.matched_terms),
            'excerpt': self.excerpt,
        })
        return row


def _normalize_text(value):
    text = str(value or '').replace('\ufeff', '').replace('\u00a0', ' ').replace('\u00ad', '')
    return re.sub(r'\s+', ' ', text).strip().lower()


def _char_ngrams(text):
    tokens = []
    for part in TOKEN_RE.findall(_normalize_text(text)):
        if re.fullmatch(r'[a-z0-9]+', part):
            tokens.append(part)
            continue
        if len(part) == 1:
            tokens.append(part)
            continue
        for n in (2, 3):
            if len(part) < n:
                continue
            tokens.extend(part[i:i + n] for i in range(len(part) - n + 1))
    return tokens


def _tokenize(text):
    tokens = _char_ngrams(text)
    raw = _normalize_text(text)
    # Domain terms are important retrieval anchors. Add them explicitly so
    # phrases such as "伤官驾杀" do not depend only on accidental bigram overlap.
    for term in DOMAIN_TERMS:
        if term.lower() in raw:
            tokens.extend([term.lower()] * 3)
    return tokens


def _query_phrases(query):
    raw = _normalize_text(query)
    phrases = [term.lower() for term in DOMAIN_TERMS if term.lower() in raw]
    phrases.extend(part for part in TOKEN_RE.findall(raw) if len(part) >= 2)
    return tuple(dict.fromkeys(sorted(phrases, key=len, reverse=True)))


def _compact_for_phrase(text):
    return ''.join(TOKEN_RE.findall(_normalize_text(text)))


def _make_excerpt(text, phrases, max_chars=120):
    clean = re.sub(r'\s+', ' ', str(text or '')).strip()
    if not clean:
        return ''
    anchors = [p for p in phrases if p and p in _normalize_text(clean)]
    if not anchors:
        return clean[:max_chars]
    anchor = max(anchors, key=len)
    idx = _normalize_text(clean).find(anchor)
    start = max(0, idx - max_chars // 3)
    end = min(len(clean), start + max_chars)
    snippet = clean[start:end].strip()
    if start > 0:
        snippet = '...' + snippet
    if end < len(clean):
        snippet += '...'
    return snippet


class ClassicsSearchIndex:
    def __init__(self, chunks):
        if not chunks:
            raise ValueError('classics corpus is empty')
        self.chunks = list(chunks)
        self._doc_tokens = []
        self._doc_counters = []
        self._doc_lengths = []
        self._doc_texts = []
        df = defaultdict(int)

        for chunk in self.chunks:
            search_text = '\n'.join([
                chunk.book,
                chunk.chapter_title,
                chunk.chapter_title,
                chunk.text,
            ])
            tokens = _tokenize(search_text)
            counts = Counter(tokens)
            self._doc_tokens.append(set(counts))
            self._doc_counters.append(counts)
            self._doc_lengths.append(sum(counts.values()))
            self._doc_texts.append(search_text)
            for token in counts:
                df[token] += 1

        self.avgdl = sum(self._doc_lengths) / len(self._doc_lengths)
        total_docs = len(self.chunks)
        self.idf = {
            token: math.log(1 + (total_docs - freq + 0.5) / (freq + 0.5))
            for token, freq in df.items()
        }

    @classmethod
    def load(cls, corpus_dir=DEFAULT_CORPUS_DIR):
        corpus_dir = Path(corpus_dir)
        if not corpus_dir.exists():
            raise FileNotFoundError(f'classics corpus directory not found: {corpus_dir}')

        chunks = []
        seen = set()
        for path in sorted(corpus_dir.glob('*.jsonl')):
            with path.open('r', encoding='utf-8') as fh:
                for line_no, line in enumerate(fh, 1):
                    if not line.strip():
                        continue
                    try:
                        chunk = ClassicChunk.from_row(json.loads(line))
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise ValueError(f'invalid corpus row: {path}:{line_no}') from exc
                    if chunk.chunk_id in seen:
                        raise ValueError(f'duplicate chunk_id: {chunk.chunk_id}')
                    seen.add(chunk.chunk_id)
                    chunks.append(chunk)
        return cls(chunks)

    def stats(self):
        books = sorted({chunk.book for chunk in self.chunks})
        return {
            'books': len(books),
            'book_names': books,
            'chunks': len(self.chunks),
            'avg_doc_tokens': round(self.avgdl, 2),
            'vocab_size': len(self.idf),
        }

    def search(self, query, k=5, book=None):
        query = str(query or '').strip()
        if not query:
            return []
        try:
            k = int(k)
        except (TypeError, ValueError):
            k = 5
        k = max(1, min(k, MAX_RESULTS))

        query_tokens = Counter(_tokenize(query))
        if not query_tokens:
            return []
        phrases = _query_phrases(query)
        compact_query = _compact_for_phrase(query)
        requested_book = str(book or '').strip()
        hits = []

        for idx, chunk in enumerate(self.chunks):
            if requested_book and chunk.book != requested_book:
                continue
            score = self._bm25_score(query_tokens, idx)
            score += self._phrase_boost(chunk, idx, phrases, compact_query)
            if score <= 0:
                continue
            matched_terms = self._matched_terms(query_tokens, phrases, idx)
            hits.append(SearchHit(
                chunk=chunk,
                score=score,
                matched_terms=matched_terms,
                excerpt=_make_excerpt(chunk.text, phrases),
            ))

        hits.sort(key=lambda hit: (-hit.score, hit.chunk.book, hit.chunk.chapter_no, hit.chunk.chunk_id))
        return hits[:k]

    def _bm25_score(self, query_tokens, doc_idx):
        k1 = 1.4
        b = 0.72
        score = 0.0
        doc_len = self._doc_lengths[doc_idx] or 1
        counts = self._doc_counters[doc_idx]
        for token, qf in query_tokens.items():
            tf = counts.get(token, 0)
            if not tf:
                continue
            denom = tf + k1 * (1 - b + b * doc_len / self.avgdl)
            score += self.idf.get(token, 0.0) * (tf * (k1 + 1) / denom) * min(qf, 3)
        return score

    def _phrase_boost(self, chunk, doc_idx, phrases, compact_query):
        if not phrases and not compact_query:
            return 0.0
        boost = 0.0
        title = _normalize_text(chunk.chapter_title)
        book = _normalize_text(chunk.book)
        text = _normalize_text(self._doc_texts[doc_idx])
        compact_doc = _compact_for_phrase(self._doc_texts[doc_idx])
        if compact_query and len(compact_query) >= 4 and compact_query in compact_doc:
            boost += 5.0 + min(len(compact_query) / 4, 4.0)
        for phrase in phrases:
            if phrase in book:
                boost += 5.0
            if phrase in title:
                boost += 4.0
            elif phrase in text:
                boost += 1.5
        return boost

    def _matched_terms(self, query_tokens, phrases, doc_idx):
        doc_tokens = self._doc_tokens[doc_idx]
        terms = [term for term in phrases if term in _normalize_text(self._doc_texts[doc_idx])]
        terms.extend(token for token in query_tokens if token in doc_tokens)
        return tuple(dict.fromkeys(terms[:10]))


@lru_cache(maxsize=4)
def load_classics_index(corpus_dir=None):
    path = Path(corpus_dir) if corpus_dir else DEFAULT_CORPUS_DIR
    return ClassicsSearchIndex.load(path)


def search_classics(query, k=5, corpus_dir=None, book=None):
    """Search the generated R1 corpus and return JSON-serializable hits."""
    index = load_classics_index(str(Path(corpus_dir)) if corpus_dir else None)
    return [hit.to_dict() for hit in index.search(query, k=k, book=book)]
