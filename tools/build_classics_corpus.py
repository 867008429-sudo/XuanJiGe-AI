#!/usr/bin/env python3
"""Build public-domain classics jsonl chunks for Agentic RAG (R1).

Input is the MIT-licensed isheng-eqi/bazi-ishengmind data directory. The
luckclub-sourced files contain modern translations and web boilerplate; this
script keeps only the original text section of each chapter.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BOOK_SOURCES = [
    {
        'filename': '滴天髓-原文-古籍典藏-luckclub.txt',
        'book': '滴天髓原文',
        'slug': 'ditiansui-yuanwen',
        'format': 'luckclub',
    },
    {
        'filename': '滴天髓阐微.txt',
        'book': '滴天髓阐微',
        'slug': 'ditiansui-chanwei',
        'format': 'chanwei',
    },
    {
        'filename': '穷通宝鉴-古籍典藏-luckclub.txt',
        'book': '穷通宝鉴',
        'slug': 'qiongtongbaojian',
        'format': 'luckclub',
    },
    {
        'filename': '三命通会-古籍典藏-luckclub.txt',
        'book': '三命通会',
        'slug': 'sanmingtonghui',
        'format': 'luckclub',
    },
    {
        'filename': '神峰通考-古籍典藏-luckclub.txt',
        'book': '神峰通考',
        'slug': 'shenfengtongkao',
        'format': 'luckclub',
    },
    {
        'filename': '渊海子平-古籍典藏-luckclub.txt',
        'book': '渊海子平',
        'slug': 'yuanhaiziping',
        'format': 'luckclub',
    },
    {
        'filename': '子平真诠-古籍典藏-luckclub.txt',
        'book': '子平真诠',
        'slug': 'zipingzhenquan',
        'format': 'luckclub',
    },
]

CHAPTER_RE = re.compile(r'^第\s*([0-9\s]+)章$')
CHANWEI_HEADING_RE = re.compile(r'^(通神论|六亲论)\s+([一二三四五六七八九十百〇零]+)、(.+?)\s*$')
STOP_MARKERS = {'白话译文', '关键词', '现代启示', '---'}
DROP_EXACT = {'《》'}


def read_text(path):
    for encoding in ('utf-8-sig', 'utf-8', 'gb18030'):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding='utf-8', errors='ignore')


def normalize_line(line):
    line = line.replace('\ufeff', '').replace('\u00a0', ' ').replace('\u00ad', '')
    return re.sub(r'\s+', ' ', line).strip()


def is_boilerplate(line):
    if not line:
        return True
    if line in DROP_EXACT:
        return True
    if 'luckclub.cn' in line or '古籍典藏' in line:
        return True
    if line.startswith('八字 · 共'):
        return True
    if re.fullmatch(r'《[^》]*》', line):
        return True
    if re.fullmatch(r'.*第\s*\d+\s*页\s*/\s*共\s*\d+\s*页.*', line):
        return True
    return False


def clean_body_lines(lines, title=''):
    cleaned = []
    for line in lines:
        line = normalize_line(line)
        if is_boilerplate(line):
            continue
        cleaned.append(line)
    if title and cleaned and cleaned[0] == title:
        cleaned = cleaned[1:]
    text = '\n'.join(cleaned).strip()
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


def make_chunk(book, slug, chapter_no, title, text, source_file, part=None):
    base_id = f'{slug}-ch{int(chapter_no):03d}'
    chunk_id = f'{base_id}-p{int(part):02d}' if part is not None else base_id
    return {
        'chunk_id': chunk_id,
        'book': book,
        'chapter_no': int(chapter_no),
        'chapter_title': title,
        'text': text,
        'source_file': source_file,
    }


def parse_luckclub_text(book, slug, source_file, text):
    chunks = []
    current = None
    in_original = False
    in_translation = False

    def finish():
        if not current:
            return
        if current['chapter_no'] == 0 or '目录' in current['title']:
            return
        body = clean_body_lines(current['body'], current['title'])
        if body:
            chunks.append(make_chunk(
                book, slug, current['chapter_no'], current['title'], body, source_file,
            ))

    for raw_line in text.splitlines():
        line = normalize_line(raw_line)
        match = CHAPTER_RE.match(line)
        if match:
            finish()
            current = {
                'chapter_no': int(re.sub(r'\s+', '', match.group(1))),
                'title': '',
                'body': [],
            }
            in_original = False
            in_translation = False
            continue
        if current is None:
            continue
        if is_boilerplate(line):
            continue
        if line == '原 文':
            in_original = True
            in_translation = False
            continue
        if line in STOP_MARKERS:
            in_translation = True
            continue
        if in_translation:
            continue
        if not in_original:
            if not current['title']:
                current['title'] = line
            continue
        current['body'].append(line)

    finish()
    return chunks


def parse_chanwei_text(book, slug, source_file, text):
    chunks = []
    current = None
    seq = 0

    def finish():
        if not current:
            return
        body = clean_body_lines(current['body'], current['title'])
        if body:
            chunks.append(make_chunk(
                book, slug, current['chapter_no'], current['title'], body, source_file,
            ))

    for raw_line in text.splitlines():
        line = normalize_line(raw_line)
        if is_boilerplate(line):
            continue
        match = CHANWEI_HEADING_RE.match(line)
        if match:
            finish()
            seq += 1
            current = {
                'chapter_no': seq,
                'title': f'{match.group(1)} {match.group(2)}、{match.group(3)}',
                'body': [],
            }
            continue
        if current is None:
            continue
        current['body'].append(line)

    finish()
    return chunks


def parse_book(source_dir, spec):
    path = Path(source_dir) / spec['filename']
    if not path.exists():
        raise FileNotFoundError(f'缺少源文件: {path}')
    text = read_text(path)
    source_id = f"{spec['slug']}.txt"
    if spec['format'] == 'chanwei':
        return parse_chanwei_text(spec['book'], spec['slug'], source_id, text)
    return parse_luckclub_text(spec['book'], spec['slug'], source_id, text)


def repo_commit(source_dir):
    try:
        root = Path(source_dir).resolve().parents[2]
        return subprocess.check_output(
            ['git', '-C', str(root), 'rev-parse', 'HEAD'],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:  # noqa: BLE001 best-effort provenance only
        return 'unknown'


def write_outputs(chunks_by_slug, output_dir, source_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stats = []
    for spec in BOOK_SOURCES:
        slug = spec['slug']
        rows = chunks_by_slug.get(slug, [])
        out_path = output_dir / f'{slug}.jsonl'
        with out_path.open('w', encoding='utf-8', newline='\n') as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + '\n')
        stats.append({
            'book': spec['book'],
            'slug': slug,
            'chunks': len(rows),
            'chars': sum(len(row['text']) for row in rows),
            'path': str(out_path.relative_to(ROOT)),
        })

    licenses = output_dir / 'LICENSES.md'
    commit = repo_commit(source_dir)
    lines = [
        '# Classics Corpus Licenses',
        '',
        'Generated by `tools/build_classics_corpus.py`.',
        '',
        '## Source',
        '',
        '- Repository: `isheng-eqi/bazi-ishengmind`',
        f'- Commit: `{commit}`',
        '- License: MIT',
        '- Source path: `skills/isheng-mind/data/`',
        '',
        '## Copyright Boundary',
        '',
        '- The generated JSONL keeps only original/classical text sections used for cultural research and retrieval.',
        '- Modern `白话译文` / `关键词` / `现代启示` sections and luckclub page boilerplate are stripped during generation.',
        '- The external source checkout stays under `.external/` and is not committed.',
        '',
        '## Generated Files',
        '',
    ]
    for row in stats:
        lines.append(f"- `{row['path']}`: {row['book']}，{row['chunks']} chunks，{row['chars']} chars")
    licenses.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return stats


def build_corpus(source_dir, output_dir=None, dry_run=False):
    chunks_by_slug = {}
    for spec in BOOK_SOURCES:
        chunks = parse_book(source_dir, spec)
        chunks_by_slug[spec['slug']] = chunks
    stats = [
        {
            'book': spec['book'],
            'slug': spec['slug'],
            'chunks': len(chunks_by_slug[spec['slug']]),
            'chars': sum(len(row['text']) for row in chunks_by_slug[spec['slug']]),
        }
        for spec in BOOK_SOURCES
    ]
    if not dry_run and output_dir:
        stats = write_outputs(chunks_by_slug, output_dir, source_dir)
    return {
        'books': len(BOOK_SOURCES),
        'chunks': sum(row['chunks'] for row in stats),
        'chars': sum(row['chars'] for row in stats),
        'dry_run': bool(dry_run),
        'stats': stats,
    }


def main():
    parser = argparse.ArgumentParser(description='Build XuanJiGe classics corpus jsonl')
    parser.add_argument(
        '--source-dir',
        default=str(ROOT / '.external' / 'bazi-ishengmind' / 'skills' / 'isheng-mind' / 'data'),
        help='source data directory from isheng-eqi/bazi-ishengmind',
    )
    parser.add_argument(
        '--output-dir',
        default=str(ROOT / 'data' / 'classics'),
        help='directory for generated jsonl files',
    )
    parser.add_argument('--dry-run', action='store_true', help='parse and print stats without writing')
    args = parser.parse_args()

    summary = build_corpus(args.source_dir, args.output_dir, dry_run=args.dry_run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
