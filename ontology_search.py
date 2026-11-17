"""Поиск по любой ONTOLIS-онтологии без словаря предметных определений.

Пайплайн: токенизация -> нормализация/лемматизация -> индексация фраз,
в т.ч. скобок -> лексическое/нечёткое сравнение -> TF-IDF cosine ->
необязательные нейросетевые embeddings. В слабых случаях возвращаем варианты.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math
from dataclasses import dataclass
from functools import lru_cache
import os
import re
from typing import Mapping

from rapidfuzz import fuzz
from question_processing import content_words, lemma, normalize, aliases

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
except ImportError:
    TfidfVectorizer = None
    cosine_similarity = None


@dataclass
class SearchResult:
    id: str
    name: str
    score: float
    origin: str


def term_variants(title: str) -> list[str]:
    """Имена и краткие формы извлекаются автоматически из ЛЮБОГО заголовка."""
    terms = [title]
    # Часто спрашивают о составной части имени без уточняющего прилагательного:
    # «нейронная сеть» внутри «искусственная нейронная сеть».
    name_parts = normalize(re.sub(r'\([^()]*\)', ' ', title)).split()
    # Аббревиатура берётся из самого заголовка: ИНС, ЭВМ и т. п.
    if len(name_parts) >= 3 and all(len(w) >= 3 for w in name_parts):
        terms.append(''.join(w[0] for w in name_parts))
    if len(name_parts) >= 3:
        for length in (2, 3):
            terms.extend(' '.join(name_parts[i:i + length])
                         for i in range(len(name_parts) - length + 1))
    # Для слитных сокращений строим возможные сокращённые формы
    # исключительно из соседних слов названия (не предметный словарь).
    if len(name_parts) >= 2:
        for first, second in zip(name_parts, name_parts[1:]):
            if len(first) >= 7 and len(second) >= 4 and not second.endswith(('ей', 'ов', 'ам', 'ям')):
                terms.append(first[:max(4, len(first) - 4)] + second)
    # Текст в скобках: «Логистическая функция (сигмоида)» → «сигмоида».
    for match in re.finditer(r'\(([^()]+)\)', title):
        inside = match.group(1)
        terms.append(inside)
        terms.extend(p.strip() for p in re.split(r'[,;/|]', inside))
    without = re.sub(r'\s*\([^()]*\)', '', title)
    if without.strip() != title.strip():
        terms.append(without)
    # Если название содержит составной термин через тире/слеш, это также алиасы.
    terms.extend(s.strip() for s in re.split(r'\s+[—–]\s+|\s*/\s*', title) if s.strip())
    for match in re.finditer(r'\b[а-яa-z]{1,5}\s*[- ]?\s*\d+\b', normalize(title)):
        terms.append(match.group(0))
    return list(dict.fromkeys(normalize(t) for t in terms if normalize(t)))


def normalized_tokens(value: str) -> set[str]:
    return set(content_words(value))


class OntologySearch:
    """Индексы пересобираются отдельно для каждого загруженного графа."""

    def __init__(self, nodes: Mapping[str, dict]):
        self.nodes = nodes
        self.terms = {}
        self.term_occurrences = Counter()
        for nid, n in nodes.items():
            attrs = n.get('attributes', {})
            labels = term_variants(n['name'])
            for key in ('aliases', 'synonyms', 'Синонимы', 'Алиасы', 'Краткое название',
                        'alternative_names', 'альтернативные названия'):
                for value in aliases(attrs.get(key)):
                    labels.extend(term_variants(value))
            labels = list(dict.fromkeys(label for label in labels if len(label) >= 2))
            self.terms[nid] = labels
            self.term_occurrences.update(set(token for lbl in labels for token in normalized_tokens(lbl)))
        self.definition_terms = {nid: normalized_tokens(
            str(node.get('attributes', {}).get('def', ''))) for nid, node in nodes.items()}
        self.definition_frequency = Counter(t for terms in self.definition_terms.values() for t in terms)
        self.node_strings = [self._search_text(nid) for nid in nodes]
        self.node_ids = list(nodes)
        self.vectorizer = None
        self.matrix = None
        if TfidfVectorizer is not None and len(nodes) >= 2:
            try:
                self.vectorizer = TfidfVectorizer(analyzer='char', ngram_range=(2, 5), min_df=1,
                                                  sublinear_tf=True, norm='l2')
                self.matrix = self.vectorizer.fit_transform(self.node_strings)
            except ValueError:
                self.vectorizer, self.matrix = None, None
        self.embedding_model = None
        self.embedding_vectors = None
        # Полностью необязательная модель, только локальный путь; ни сети,
        # ни скачивания весов при открытии ONTOLIS не требуется.
        model_path = os.environ.get('ONTOTUTOR_EMBEDDING_MODEL', '').strip()
        if model_path and os.path.isdir(model_path):
            try:
                from sentence_transformers import SentenceTransformer
                self.embedding_model = SentenceTransformer(model_path, local_files_only=True)
                self.embedding_vectors = self.embedding_model.encode(
                    self.node_strings, normalize_embeddings=True, show_progress_bar=False)
            except (ImportError, OSError, ValueError):
                self.embedding_model = None
                self.embedding_vectors = None

    def _search_text(self, nid: str) -> str:
        node = self.nodes[nid]
        # Для TF-IDF повторяем заголовки и варианты, а текст определения
        # используем как контекст (не как источник доказательства совпадения).
        definition = str(node['attributes'].get('def') or '')
        return ' '.join(self.terms[nid] * 2 + [normalize(definition)])

    @lru_cache(maxsize=2048)
    def _vector_scores(self, question: str) -> dict[str, float]:
        if not self.vectorizer or self.matrix is None:
            return {}
        vector = self.vectorizer.transform([normalize(question)])
        similarities = cosine_similarity(vector, self.matrix)[0]
        return {nid: float(score) for nid, score in zip(self.node_ids, similarities)}

    @lru_cache(maxsize=256)
    def _embedding_scores(self, question: str) -> dict[str, float]:
        if self.embedding_model is None:
            return {}
        vector = self.embedding_model.encode([question], normalize_embeddings=True, show_progress_bar=False)[0]
        similarities = self.embedding_vectors @ vector
        return {nid: max(0.0, float(score)) for nid, score in zip(self.node_ids, similarities)}

    def lookup(self, question: str, preferred_ids: set[str] | None = None, limit: int = 5) -> list[SearchResult]:
        q = normalize(question)
        if not q:
            return []
        qt = normalized_tokens(q)
        if not qt:
            return []
        descriptive = bool(re.search(r'\b(как называется|как называют|что за|найди понятие|какая .* которая|какой .* который)\b', q))
        vector = self._vector_scores(q)
        emb = self._embedding_scores(q)
        result: list[SearchResult] = []
        padded = f' {q} '
        for nid, node in self.nodes.items():
            strongest = 0.0
            how = 'поиск по термину'
            for term in self.terms[nid]:
                if f' {term} ' in padded:
                    score = 1.0 if normalize(node['name']) == term else .97
                    # При совпадении аббревиатур нескольких заголовков
                    # предпочитаем именительный термин генитивным сочетаниям.
                    # Иначе «ИНС» двусмысленно для названий разных категорий.
                    words_in_name = normalize(node['name']).split()
                    if len(term) <= 5 and len(words_in_name) >= 3 and term == ''.join(w[0] for w in words_in_name):
                        if words_in_name[-1].endswith(('ей', 'ов', 'ях', 'ых')):
                            score -= .04
                    # Специализированный вариант в скобках не менее весомый,
                    # чем длинный заголовок: «сигмоида» → конкретный узел.
                    if score > strongest:
                        strongest, how = score, 'название или вариант в скобках'
                    continue
                tt = normalized_tokens(term)
                if not tt:
                    continue
                overlap = qt & tt
                if overlap and len(overlap) == len(tt):
                    score = .94 if len(tt) >= 2 else .90
                    if score > strongest:
                        strongest, how = score, 'лемматизация названия'
                    continue
                if len(tt) > 1 and len(overlap) > 1:
                    score = .61 + .23 * (len(overlap) / len(tt))
                    if score > strongest:
                        strongest, how = score, 'частичное совпадение'
                if len(tt) == 1 and len(overlap) == 1:
                    token = next(iter(tt))
                    # В отличие от общих слов, редкий элемент названия допустим.
                    unique = self.term_occurrences[token] == 1
                    score = .85 if unique else .56
                    if score > strongest:
                        strongest, how = score, 'отдельный термин в названии'
                # Опечатки и сокращения: сравниваем только содержательные слова.
                if len(qt) <= 7:
                    for token in tt:
                        if len(token) < 5:
                            continue
                        fuzzy = max((fuzz.ratio(token, w) / 100 for w in qt if len(w) >= 4), default=0)
                        if fuzzy >= .80:
                            score = .60 + .29 * ((fuzzy - .80) / .20)
                            if fuzzy >= .95:
                                score = .89 if self.term_occurrences[token] == 1 else .66
                            if score > strongest:
                                strongest, how = score, 'похожее написание'
                # Слитное сокращение: «нейросеть» / «нейронная сеть».
                if len(tt) >= 2 and len(qt) == 1:
                    joined = ''.join(term.split())
                    joined_q = next(iter(qt))
                    ratio = fuzz.ratio(joined_q, joined) / 100
                    if ratio >= .75 and len(joined_q) >= 6:
                        score = .78 + (ratio - .75) * .30
                        if score > strongest:
                            strongest, how = score, 'сокращённое написание'
            # При поиске понятия по его описанию короткое слово в вопросе
            # может оказаться лишь контекстом («сигнал нейрона»), а не ответом.
            if descriptive and len(qt) >= 4 and len(normalized_tokens(node['name'])) == 1:
                strongest = min(strongest, .69)
            # Поиск по содержимому определений с IDF: не доверяем одному
            # случайному слову, но учитываем несколько информативных совпадений.
            def_tokens = self.definition_terms[nid]
            common = qt & def_tokens
            if len(common) >= 3 and len(qt) >= 4:
                weights = lambda terms: sum(1 + math.log((len(self.nodes) + 1) /
                                    (self.definition_frequency.get(t, 0) + 1)) for t in terms)
                coverage = weights(common) / weights(qt)
                if coverage >= .46:
                    desc_score = min(.91, .66 + .28 * coverage)
                    if desc_score > strongest:
                        strongest, how = desc_score, 'сходство с определением'
            # Лексическое представление и контекст определения уточняют
            # ранжирование; оно не должно уверенно выдумывать имя термина.
            sim = vector.get(nid, 0)
            if strongest < .7 and sim >= .28:
                weighted = min(.72, sim * 1.15)
                if weighted > strongest:
                    strongest, how = weighted, 'TF-IDF, косинусное сходство'
            semantic = emb.get(nid, 0)
            if strongest < .8 and semantic >= .55:
                weighted = min(.81, .40 + .52 * semantic)
                if weighted > strongest:
                    strongest, how = weighted, 'семантические эмбеддинги'
            if strongest >= .20:
                result.append(SearchResult(nid, node['name'], round(strongest, 4), how))
        # Предпочтение выбранной области только при близких оценках.
        result.sort(key=lambda r: (-r.score, 0 if preferred_ids and r.id in preferred_ids else 1,
                                   -len(normalize(r.name).split()), r.name, r.id))
        return result[:limit]

    def resolve(self, question: str, current_id: str | None = None,
                preferred_ids: set[str] | None = None) -> tuple[str | None, list[SearchResult]]:
        result = self.lookup(question, preferred_ids, limit=8)
        if not result:
            return None, []
        high = result[0].score
        # Несколько одинаковых названий с равным счётом — один термин.
        tops = [r for r in result if r.score >= high - .025]
        if high >= .77:
            if current_id and any(r.id == current_id for r in tops):
                return current_id, result
            if preferred_ids:
                picked = next((r for r in tops if r.id in preferred_ids), None)
                if picked:
                    return picked.id, result
            distinctive = {normalize(r.name) for r in tops}
            if len(distinctive) == 1 or len(tops) == 1:
                return min(tops, key=lambda r: (0, int(r.id)) if r.id.isdigit() else (1, r.id)).id, result
        return None, result
