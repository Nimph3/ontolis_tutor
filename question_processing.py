"""Прозрачная обработка русскоязычных вопросов без внешней LLM.

Поддерживаются словоформы (с необязательным pymorphy3), синонимы,
небольшие опечатки и типы учебных запросов. Низкая уверенность -> уточнение.
"""
from __future__ import annotations

from difflib import SequenceMatcher
from functools import lru_cache
import re
import unicodedata

try:
    import pymorphy3
    _morph = pymorphy3.MorphAnalyzer()
except (ImportError, RuntimeError, ValueError):
    _morph = None


def normalize(value: str) -> str:
    return ' '.join(re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е')))


@lru_cache(maxsize=12000)
def lemma(word: str) -> str:
    if _morph and re.fullmatch(r'[а-я]+', word):
        return _morph.parse(word)[0].normal_form.replace('ё', 'е')
    # Резервный режим, если pymorphy3 не установлен.
    endings = ('иями', 'остью', 'ением', 'ениями', 'ского', 'скими', 'ского', 'ного',
               'ному', 'ными', 'ными', 'иями', 'ями', 'ами', 'ого', 'ему', 'ыми', 'ими', 'ую', 'юю',
               'иях', 'ениях', 'овых', 'ова', 'ать', 'ять', 'ить', 'ых', 'их', 'ой',
               'ый', 'ий', 'ая', 'яя', 'ые', 'ие', 'ым', 'им', 'ом', 'ем', 'ам', 'ям',
               'ах', 'ях', 'ов', 'ев', 'ей', 'е', 'а', 'я', 'ы', 'и', 'у', 'ю', 'ь')
    for end in endings:
        if len(word) > len(end) + 3 and word.endswith(end):
            return word[:-len(end)]
    return word


STOPWORDS = set(normalize('''что такое это значит как какой какая какие какого какова каковы
    расскажи объясни опиши назови почему зачем для чего из о об про с со по на в во у к
    и или а же ли то мне нам пожалуйста можно нужно работает бывает есть его ее оно
    он она они этой этого этих такое понятие термин такое определение дай
    отличает отличается между сравни отличие разница пример привести расскажите''').split())

# Пары допускаются только как подсказки к поиску понятий; они не добавляют фактов в ответ.
SYNONYMS = {
    'нейросеть': ('нейронная сеть', 'искусственная нейронная сеть'),
    'инс': ('искусственная нейронная сеть',),
    'ии': ('искусственный интеллект',),
    'машинное обучение': ('ml',),
    'нейрон': ('искусственный нейрон',),
    'нейросимулятор': ('симулятор нейронных сетей',),
}


def aliases(value) -> list[str]:
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    if isinstance(value, str):
        return [part.strip() for part in re.split(r'[,;\n]+', value) if part.strip()]
    return []


def words(text: str) -> list[str]:
    return [lemma(w) for w in normalize(text).split()]


def content_words(text: str) -> list[str]:
    return [w for w in words(text) if w not in STOPWORDS and len(w) > 2]


def detect_intent(question: str) -> str:
    q = normalize(question)
    patterns = (
        ('compare', ('чем отличается', 'в чем разница', 'сравни', 'сравнение', 'различие', 'отличие', 'разница между')),
        ('composition', ('из чего', 'состоит', 'компонент', 'части', 'включает', 'содержит', 'структура')),
        ('types', ('какие виды', 'какие бывают', 'типы', 'разновидности', 'подвиды', 'примеры')),
        ('purpose', ('зачем', 'для чего', 'назначение', 'для чего нужен', 'для чего нужна', 'применяется', 'используется', 'решает', 'задачи', 'помогает')),
        ('input', ('вход', 'подается', 'получает на вход')),
        ('output', ('выход', 'результат', 'возвращает', 'получается')),
        ('relations', ('связаны', 'связано', 'связь', 'отношения', 'зависит')),
        ('operation', ('как работает', 'как устроен', 'как устроена', 'как действует', 'каким образом')),
    )
    for kind, variants in patterns:
        if any(variant in q for variant in variants):
            return kind
    return 'definition'


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def phrase_score(question: str, phrase: str) -> float:
    """Сходство термина с вопросом. Не искать по совпадению одного общесловарного слова."""
    norm_q, norm_phrase = normalize(question), normalize(phrase)
    if not norm_phrase or not norm_q:
        return 0.0
    padded = f' {norm_q} '
    if f' {norm_phrase} ' in padded:
        return 1.0
    q_terms = set(content_words(norm_q))
    term_parts = content_words(norm_phrase)
    if not term_parts:
        return 0.0
    hits = sum(t in q_terms for t in term_parts)
    if hits == len(term_parts):
        return 0.94 if len(term_parts) > 1 else 0.87
    if len(term_parts) >= 3 and hits >= 2 and hits / len(term_parts) >= .66:
        return .73
    if len(term_parts) == 2 and hits == 1:
        return 0.0  # «нейрон» не равен «нейронной сети»
    # Незначительная опечатка в отдельном термине при явно коротком вводе.
    q_tokens = content_words(norm_q)
    if len(term_parts) == 1:
        best = max((similarity(term_parts[0], w) for w in q_tokens), default=0)
        return .70 if len(term_parts[0]) >= 5 and best >= .87 else 0
    return 0.0
