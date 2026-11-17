"""Перенос ОПРЕДЕЛЕНИЙ из таблицы DOCX в атрибут nodes[].attributes.definition ONTOLIS.

Ни один термин или текст определения не задаётся в исходном коде. Если подходящая
строка неоднозначна, запись не заполняется; соответствие уточняется через CSV.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

from docx import Document
from rapidfuzz import fuzz, process


def normalize(text: str) -> str:
    text = unicodedata.normalize('NFKC', str(text)).casefold().replace('ё', 'е')
    return ' '.join(re.findall(r'[a-zа-я0-9]+', text))


def title_forms(title: str) -> list[str]:
    """Варианты названия, выведенные из текста, а не из конкретной онтологии."""
    variants = [title]
    stripped = re.sub(r'\s*\([^()]*\)', '', title).strip()
    if stripped and stripped != title:
        variants.append(stripped)
    # «Персептрон (двухслойный, MLP)» -> «Персептрон (двухслойный)».
    shortened = re.sub(r'\(([^(),]+),\s*[^()]+\)', r'(\1)', title)
    if shortened != title:
        variants.append(shortened)
    return list(dict.fromkeys(normalize(v) for v in variants if normalize(v)))


def read_document(path: Path) -> dict[str, list[dict]]:
    records = defaultdict(list)
    for table in Document(path).tables:
        header = None
        first = 0
        for index, row in enumerate(table.rows[:5]):
            headers = [normalize(c.text) for c in row.cells]
            if 'понятие' in headers and 'определение' in headers:
                header = {k: headers.index(k) for k in ('понятие', 'определение')}
                header['категория'] = headers.index('категория') if 'категория' in headers else None
                first = index + 1
                break
        if header is None:
            continue
        for row_idx, row in enumerate(table.rows[first:], first + 1):
            cells = [c.text.strip() for c in row.cells]
            if len(cells) <= max(header['понятие'], header['определение']):
                continue
            term, definition = cells[header['понятие']], cells[header['определение']]
            if not term or not definition:
                continue
            category_col = header['категория']
            category = cells[category_col] if category_col is not None and category_col < len(cells) else ''
            entry = {'term': term, 'category': category, 'definition': definition.strip(), 'row': row_idx}
            if entry not in records[normalize(term)]:
                records[normalize(term)].append(entry)
    if not records:
        raise ValueError('В DOCX не найдена таблица с колонками «Понятие» и «Определение».')
    return dict(records)


def read_overrides(path: Path | None) -> dict[str, dict]:
    if path is None:
        return {}
    result = {}
    with path.open(encoding='utf-8-sig', newline='') as file:
        for row in csv.DictReader(file):
            node_id = str(row.get('node_id', '')).strip()
            if node_id:
                result[node_id] = {'term': row.get('doc_term', '').strip(),
                                   'category': row.get('doc_category', '').strip()}
    return result


def resolve(name: str, node_id: str, definitions: dict, overrides: dict,
            fuzzy_threshold: float = 90) -> tuple[str, dict | None, str]:
    override = overrides.get(node_id, {})
    names = [normalize(override['term'])] if override.get('term') else title_forms(name)
    candidates = []
    how = ''
    for form in names:
        if form in definitions:
            candidates = definitions[form]
            how = 'точное совпадение / вариант названия'
            break
    if not candidates:
        # Автосопоставление допускается только для уверенного единственного варианта.
        found = process.extract(names[0], definitions.keys(), scorer=fuzz.ratio, limit=3)
        if found and found[0][1] >= fuzzy_threshold and (len(found) == 1 or found[0][1] - found[1][1] >= 4):
            candidates = definitions[found[0][0]]
            how = f'похожее название ({found[0][1]:.1f}%)'
    if not candidates:
        possible = process.extract(names[0], definitions.keys(), scorer=fuzz.ratio, limit=3)
        suggestions = '; '.join(definitions[k][0]['term'] for k, _, _ in possible)
        return 'unmatched', None, suggestions
    if override.get('category'):
        candidates = [c for c in candidates if normalize(c['category']) == normalize(override['category'])]
        if not candidates:
            return 'ambiguous', None, 'Категория из CSV не найдена в DOCX.'
    unique = {c['definition'] for c in candidates}
    if len(unique) != 1:
        return 'ambiguous', None, 'Найдено несколько определений; укажите doc_category в CSV: ' + '; '.join(c['category'] for c in candidates)
    return 'matched', candidates[0], how


def format_definition(term: str, doc_text: str) -> str:
    value = re.sub(r'\s+', ' ', doc_text).strip()
    value = re.sub(r'^это\b\s*[:—–-]?\s*', '', value, count=1, flags=re.I)
    # Содержательная часть целиком из DOCX; только первая буква приводится
    # к нижнему регистру для единообразия записи «Термин — это ...».
    if value and value[0].isalpha():
        value = value[0].lower() + value[1:]
    return f'{term} — это {value}'


def enrich(ontology: dict, definitions: dict, overrides: dict) -> list[dict]:
    if not isinstance(ontology, dict) or not isinstance(ontology.get('nodes'), list):
        raise ValueError('Ожидается экспорт ONTOLIS с массивом nodes.')
    report = []
    for node in ontology['nodes']:
        if not isinstance(node, dict) or not str(node.get('name', '')).strip():
            raise ValueError('Обнаружена вершина без названия.')
        nid, title = str(node.get('id', '')), str(node['name']).strip()
        status, record, detail = resolve(title, nid, definitions, overrides)
        if status == 'matched':
            attrs = node.setdefault('attributes', {})
            if not isinstance(attrs, dict):
                raise ValueError(f'У узла {nid} attributes должен быть JSON-объектом.')
            # DOCX — единственный источник. Обновляем и ранее заполненные значения.
            attrs.pop('def', None)
            attrs.pop('Определение', None)
            attrs['definition'] = format_definition(title, record['definition'])
        report.append({'node_id': nid, 'node_name': title, 'status': status,
                       'doc_term': record['term'] if record else '',
                       'doc_category': record['category'] if record else '',
                       'detail': detail})
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ont', '--json', dest='ont', required=True, help='Исходный JSON-экспорт ONTOLIS (.ont)')
    parser.add_argument('--docx', required=True, help='Таблица с определениями в DOCX')
    parser.add_argument('--out', required=True, help='Результат в формате .ont')
    parser.add_argument('--report', default='definitions_report.csv')
    parser.add_argument('--overrides', help='CSV: node_id,doc_term,doc_category (без текстов определений)')
    args = parser.parse_args()
    if Path(args.ont).resolve() == Path(args.out).resolve():
        parser.error('Исходный и выходной файлы должны отличаться.')
    raw = json.loads(Path(args.ont).read_text(encoding='utf-8-sig'))
    report = enrich(raw, read_document(Path(args.docx)), read_overrides(Path(args.overrides) if args.overrides else None))
    Path(args.out).write_text(json.dumps(raw, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    with Path(args.report).open('w', encoding='utf-8-sig', newline='') as fp:
        writer = csv.DictWriter(fp, fieldnames=['node_id','node_name','status','doc_term','doc_category','detail'])
        writer.writeheader(); writer.writerows(report)
    print(f'Обработано узлов: {len(report)}; заполнено из DOCX: {sum(r["status"] == "matched" for r in report)}; '+
          f'требуют проверки: {sum(r["status"] != "matched" for r in report)}')
    print(f'ONTOLIS: {args.out}\nОтчёт CSV: {args.report}')

if __name__ == '__main__':
    main()
