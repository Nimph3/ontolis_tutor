"""Онтологические тройки и объяснения без LLM и предметных словарей.

Единственный источник фактов: nodes/relations загруженного файла ONTOLIS.
Интерпретируются только документированные *роли* рёбер (has, is_a,
use, input и т. п.). Для неизвестного типа используется нейтральная фраза.
"""
from __future__ import annotations

from collections import defaultdict, deque
from question_processing import detect_intent, normalize


def sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text.endswith(('.', '!', '?', '…')) else text + '.'


class OntologyReasoner:
    def __init__(self, graph):
        self.g = graph

    def triples(self, center: str | None = None, depth: int = 1, limit: int = 250) -> list[dict]:
        """(subject, predicate, object): направление ВСЕГДА исходное из ONTOLIS."""
        if center is not None and center not in self.g.nodes:
            raise KeyError(center)
        distance = {}
        if center is not None:
            distance = {center: 0}
            queue = deque([center])
            while queue:
                current = queue.popleft()
                if distance[current] >= depth:
                    continue
                for r in self.g.outgoing[current] + self.g.incoming[current]:
                    other = r['destination_node_id'] if r['source_node_id'] == current else r['source_node_id']
                    if other not in distance:
                        distance[other] = distance[current] + 1
                        queue.append(other)
        facts = []
        for r in self.g.relations.values():
            a, b = r['source_node_id'], r['destination_node_id']
            if center is not None and (a not in distance or b not in distance):
                continue
            facts.append({
                'relation_id': r['id'],
                'subject_id': a, 'subject': self.g.nodes[a]['name'],
                'predicate': r['name'], 'role': self.g.relation_kind(r),
                'object_id': b, 'object': self.g.nodes[b]['name'],
                'distance': min(distance.get(a, 0), distance.get(b, 0)) if center else 0,
            })
            if len(facts) >= limit:
                break
        return facts

    @staticmethod
    def _other(f: dict, node_id: str) -> str:
        return f['object_id'] if f['subject_id'] == node_id else f['subject_id']

    def _direct(self, node_id: str) -> list[dict]:
        return [f for f in self.triples(node_id, depth=1, limit=5000)
                if node_id in (f['subject_id'], f['object_id'])]

    def select_facts(self, node_id: str, intent: str, max_facts: int = 12) -> list[dict]:
        direct = self._direct(node_id)

        def weight(f):
            role, a, b = f['role'], f['subject_id'], f['object_id']
            if intent == 'composition':
                if (role == 'has' and a == node_id) or (role == 'a_part_of' and b == node_id): return 0
                return 90
            if intent == 'types':
                if role in ('is_a', 'is_instance') and b == node_id: return 0
                return 90
            if intent == 'purpose':
                if role in ('used_for', 'use') and a == node_id: return 0
                if role in ('used_for', 'use') and b == node_id: return 3
                return 90
            if intent == 'input':
                return 0 if role == 'input' else 90
            if intent == 'output':
                return 0 if role == 'output' else 90
            if intent == 'operation':
                return {'has': 0, 'a_part_of': 1, 'is_instance': 2,
                        'input': 3, 'use': 4, 'output': 5, 'used_for': 6,
                        'is_a': 7}.get(role, 9)
            if intent == 'relations': return 0
            return {'has': 0, 'is_a': 1, 'is_instance': 1,
                    'a_part_of': 2, 'use': 3, 'used_for': 4,
                    'input': 5, 'output': 6}.get(role, 8)

        ordered = sorted(direct, key=lambda f: (weight(f), str(f['relation_id'])))
        if intent in ('purpose', 'composition', 'types', 'input', 'output'):
            ordered = [f for f in ordered if weight(f) < 90]
        chosen = ordered[:max_facts]
        # Для вопроса о назначении общего понятия рассматриваем его
        # конкретные подклассы/экземпляры (не глубже двух переходов).
        # Это НЕ означает, что все свойства наследуются автоматически:
        # в объяснении называем именно конкретный узел-носитель связи.
        if intent == 'purpose' and not chosen:
            queue = deque([(node_id, 0)])
            seen_nodes = {node_id}
            seen_facts = set()
            while queue and len(chosen) < max_facts:
                current, distance = queue.popleft()
                if distance and distance <= 2:
                    for f in self._direct(current):
                        if (f['subject_id'] == current and f['role'] in ('used_for', 'use')
                                and f['relation_id'] not in seen_facts):
                            chosen.append(f)
                            seen_facts.add(f['relation_id'])
                            if len(chosen) >= max_facts:
                                break
                if distance >= 2:
                    continue
                for f in self._direct(current):
                    if f['role'] in ('is_a', 'is_instance', 'is_instance_of') and f['object_id'] == current:
                        child = f['subject_id']
                        if child not in seen_nodes:
                            seen_nodes.add(child)
                            queue.append((child, distance + 1))
        # Для «Как работает ...» полезна соседняя часть структуры:
        # сеть -> слой нейронов -> нейрон, независимо от предметной области.
        if intent == 'operation':
            parts = []
            for f in chosen:
                if f['role'] == 'has' and f['subject_id'] == node_id:
                    parts.append(f['object_id'])
                elif f['role'] == 'a_part_of' and f['object_id'] == node_id:
                    parts.append(f['subject_id'])
            seen = {f['relation_id'] for f in chosen}
            for part in parts[:5]:
                for f in self._direct(part):
                    if f['relation_id'] in seen:
                        continue
                    if (f['role'] == 'has' and f['subject_id'] == part) or (f['role'] == 'a_part_of' and f['object_id'] == part):
                        chosen.append(f)
                        seen.add(f['relation_id'])
                        if len(chosen) >= max_facts:
                            break
                if len(chosen) >= max_facts:
                    break
        return chosen

    def graph_path(self, start: str, end: str, max_hops: int = 3) -> list[dict]:
        """Кратчайшая цепочка исходных отношений между двумя понятиями."""
        if start == end:
            return []
        queue = deque([(start, [])])
        visited = {start}
        while queue:
            current, path = queue.popleft()
            if len(path) == max_hops:
                continue
            for f in self._direct(current):
                other = self._other(f, current)
                if other in visited:
                    continue
                extended = path + [f]
                if other == end:
                    return extended
                visited.add(other)
                queue.append((other, extended))
        return []

    def _describe_fact(self, f: dict) -> str:
        s, o, role = f['subject'], f['object'], f['role']
        a, b = f'«{s}»', f'«{o}»'
        if role == 'has': return f'{a} включает {b}'
        if role == 'a_part_of': return f'{a} входит в состав {b}'
        if role == 'is_a': return f'{a} является разновидностью {b}'
        if role in ('is_instance', 'is_instance_of'): return f'{a} является экземпляром {b}'
        if role == 'use': return f'{a} использует {b}'
        if role == 'used_for': return f'{a} используется для {b}'
        if role == 'input': return f'{a} выступает входом для {b}'
        if role == 'output': return f'{b} указан как выход для {a}'
        if role == 'same_as': return f'{a} сопоставлено с {b}'
        return f'между {a} и {b} задано отношение «{f["predicate"] or "без названия"}»'

    def explain(self, node_id: str, question: str, other_id: str | None = None) -> tuple[str, list[dict], list[dict]]:
        """Вопрос -> определение -> связное объяснение; факты только из графа."""
        g = self.g
        node = g.nodes[node_id]
        name = node['name']
        intent = detect_intent(question)
        definition = g.definition_text(node)
        source = [{'node_id': node_id, 'field': node['_definition_source']}]
        heading = f'Вопрос: {question.strip()}\nРаспознанное понятие: «{name}».'
        definition_block = (f'Определение: {name} — {sentence(definition)}'
                            if node['attributes'].get('def') else f'Определение: {definition}')
        triples = []
        statements = []

        if intent == 'compare' and other_id and other_id != node_id:
            second = g.nodes[other_id]
            definition_block += ('\nОпределение второго понятия: '
                                 + f'{second["name"]} — {sentence(g.definition_text(second))}')
            source.append({'node_id': other_id, 'field': second['_definition_source']})
            triples = self.graph_path(node_id, other_id, max_hops=3)
            if triples:
                statements.append('В графе знаний между ними прослеживается следующая последовательность связей: '
                                  + '. '.join(sentence(self._describe_fact(f)) for f in triples))
            else:
                statements.append('Прямая или короткая цепочка отношений между понятиями не задана. '
                                  'Поэтому различия можно установить только по приведённым определениям.')
        else:
            triples = self.select_facts(node_id, intent)
            if intent == 'operation':
                statements.append('Принцип работы можно пояснить через структуру и взаимодействия, '
                                  'которые зафиксированы в графе знаний.')
            groups = defaultdict(list)
            for f in triples:
                groups[(f['role'], f['subject_id'], f['object_id'] == node_id)].append(f)
            used = set()
            # Группируем однородные связи для связного текста вместо списка
            # несвязанных «сосед: роль».
            for f in triples:
                if f['relation_id'] in used:
                    continue
                role = f['role']
                if role == 'has' and f['subject_id'] == node_id:
                    same = [v for v in triples if v['role'] == role and v['subject_id'] == node_id
                            and v['relation_id'] not in used]
                    if len(same) > 1:
                        statements.append(f'В состав «{name}» входят ' + ', '.join(f'«{v["object"]}»' for v in same) + '.')
                        used.update(v['relation_id'] for v in same)
                        continue
                if role in ('use', 'used_for') and f['subject_id'] == node_id:
                    same = [v for v in triples if v['role'] == role and v['subject_id'] == node_id
                            and v['relation_id'] not in used]
                    if len(same) > 1:
                        if role == 'use':
                            statements.append(f'Для этого «{name}» использует ' + ', '.join(f'«{v["object"]}»' for v in same) + '.')
                        else:
                            statements.append(f'В онтологии применение «{name}» связано с такими задачами, как '
                                              + ', '.join(f'«{v["object"]}»' for v in same) + '.')
                        used.update(v['relation_id'] for v in same)
                        continue
                if role == 'a_part_of' and f['object_id'] == node_id:
                    same = [v for v in triples if v['role'] == role and v['object_id'] == node_id
                            and v['relation_id'] not in used]
                    if len(same) > 1:
                        statements.append(f'Частями «{name}» являются ' + ', '.join(f'«{v["subject"]}»' for v in same) + '.')
                        used.update(v['relation_id'] for v in same)
                        continue
                statements.append(sentence(self._describe_fact(f)))
                used.add(f['relation_id'])
            if intent == 'operation' and triples:
                explained = set()
                for f in triples:
                    if len(explained) >= 3:
                        break
                    if f['role'] not in ('has', 'a_part_of', 'input', 'output'):
                        continue
                    for near_id in (f['subject_id'], f['object_id']):
                        if near_id == node_id or near_id in explained:
                            continue
                        child = g.nodes[near_id]
                        if not child['attributes'].get('def'):
                            continue
                        meaning = g.definition_text(child).strip()
                        # Определения являются данными онтологии. Не обрезаем
                        # их до фиксированной длины: пользователь должен
                        # получать полный исходный текст.
                        statements.append(f'«{child["name"]}» при этом означает следующее: {sentence(meaning)}')
                        explained.add(near_id)
                        source.append({'node_id': near_id, 'field': child['_definition_source']})
                        if len(explained) >= 3:
                            break
            if not triples:
                if intent == 'definition':
                    statements.append('Других связей, дополняющих определение, для этого понятия не найдено.')
                else:
                    statements.append('В загруженном графе нет отношений, позволяющих достоверно ответить '
                                      'на эту часть вопроса. Можно изучить определение или открыть соседние понятия.')
        for f in triples:
            source.append({
                'relation': g.describe_relation(g.relations[f['relation_id']]),
                'node_id': f['object_id'], 'field': 'name',
            })
        answer_title = 'Ответ:\nСостав: ' if intent == 'composition' else 'Ответ: '
        return heading + '\n\n' + definition_block + '\n\n' + answer_title + ' '.join(statements), source, triples
