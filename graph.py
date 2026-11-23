"""Онтология ONTOLIS: индексы, семантические связи и учебный DFS-обход."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import re
import unicodedata
from question_processing import detect_intent, normalize as normalize_query
from ontology_search import OntologySearch
from ontology_reasoning import OntologyReasoner


@dataclass(frozen=True)
class Step:
    node_id: str
    via_relation_id: str | None = None
    via_relation_type: str | None = None
    parent_id: str | None = None


# ONTOLIS хранит связи в их СЕМАНТИЧЕСКОМ направлении.
# Для движения от общей темы к частной некоторые из них нужно инвертировать.
# 'use', 'used_for', 'input' и 'output' добавляют связанные учебные ветви,
# но при составлении ответа не считаются отношениями «является частью».
TEACHING_EDGES = {
    "has": ("forward", 0),
    "a_part_of": ("backward", 0),
    "is_a": ("backward", 1),
    "is_instance": ("backward", 2),
    "used_for": ("forward", 3),
    "use": ("forward", 4),
    "output": ("forward", 5),
    "input": ("backward", 6),
}


# Распространённые названия отношений у различных экспортов ONTOLIS.
# Это универсальные роли, а не знания конкретной предметной области.
RELATION_ALIASES = {
    'is_instance_of': 'is_instance',
    'instance_of': 'is_instance',
    'is_a_kind_of': 'is_a',
    'part_of': 'a_part_of',
    'has_part': 'has',
    'consists_of': 'has',
    'uses': 'use',
    'use_for': 'used_for',
}


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold().replace("ё", "е")
    return " ".join(re.findall(r"[\w]+", s, flags=re.UNICODE))


def word_stem(s: str) -> str:
    for suf in ("иями", "ями", "ами", "иями", "ого", "ему", "ыми", "ими", "иях", "овых", "ова", "ами", "ах", "ях", "ов", "ев", "ей", "ой", "ый", "ий", "ая", "яя", "ые", "ие", "ым", "им", "ом", "ем", "ам", "ям", "ам", "е", "а", "я", "ы", "и", "у", "ю", "ь"):
        if len(s) > max(len(suf) + 3, 5) and s.endswith(suf):
            return s[: -len(suf)]
    return s


class Ontology:
    """Один независимый граф; его маршрут и таблица отношений не глобальны."""

    def __init__(
        self,
        path: Path,
        *,
        default_order: dict[str, list[str]] | None = None,
        relation_mapping: dict[str, str] | None = None,
    ):
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Файл должен содержать корректный UTF-8 JSON в формате ONTOLIS.") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("nodes"), list) or not isinstance(raw.get("relations"), list):
            raise ValueError("Нужен JSON-объект ONTOLIS с массивами nodes и relations.")
        if not 1 <= len(raw["nodes"]) <= 1000:
            raise ValueError("Допустимо от 1 до 1000 понятий (nodes).")
        if len(raw["relations"]) > 5000:
            raise ValueError("Допустимо не более 5000 связей (relations).")

        self.nodes: dict[str, dict] = {}
        for index, item in enumerate(raw["nodes"], 1):
            if not isinstance(item, dict) or not isinstance(item.get("id"), (int, str)):
                raise ValueError(f"У понятия №{index} отсутствует корректный id.")
            nid = str(item["id"])
            name = item.get("name")
            if not nid or nid in self.nodes or not isinstance(name, str) or not name.strip():
                raise ValueError(f"Неверный/повторный id или пустое название понятия №{index}.")
            attrs = item.get("attributes") or {}
            if not isinstance(attrs, dict):
                raise ValueError(f"У понятия «{name}» поле attributes должно быть объектом.")
            # Нормализуем лишь внутреннее представление: файл ONTOLIS не меняем.
            attrs = dict(attrs)
            def_key = next((key for key in ("definition", "Definition", "def", "Определение", "определение", "Описание", "description")
                            if str(attrs.get(key) or "").strip()), None)
            if def_key and def_key != "def":
                attrs["def"] = str(attrs[def_key]).strip()
            node = dict(item, id=nid, name=name.strip(), attributes=attrs)
            node["_definition_source"] = f"attributes.{def_key}" if def_key else "attributes.def"
            self.nodes[nid] = node

        self.relations: dict[str, dict] = {}
        for index, item in enumerate(raw["relations"], 1):
            if not isinstance(item, dict) or not all(k in item for k in ("id", "name", "source_node_id", "destination_node_id")):
                raise ValueError(f"Связь №{index} должна иметь id, name, source_node_id и destination_node_id.")
            rid = str(item["id"])
            src, dst = str(item["source_node_id"]), str(item["destination_node_id"])
            if not rid or rid in self.relations or not isinstance(item["name"], str):
                raise ValueError(f"Повторный id или неверное название связи №{index}.")
            if src not in self.nodes or dst not in self.nodes:
                raise ValueError(f"Связь #{rid} ссылается на отсутствующее понятие ({src} → {dst}).")
            self.relations[rid] = dict(item, id=rid, source_node_id=src, destination_node_id=dst)

        self.search = OntologySearch(self.nodes)
        self.definitions_count = sum(bool(str(n["attributes"].get("def") or "").strip()) for n in self.nodes.values())
        self.curriculum_order = dict(default_order or {})
        order_file = path.parent / "curriculum_order.json"
        if order_file.exists():
            supplied = json.loads(order_file.read_text(encoding="utf-8"))
            if not isinstance(supplied, dict):
                raise ValueError("curriculum_order.json: ожидается объект {id: [id, ...]}")
            for parent, children in supplied.items():
                if parent not in self.nodes or not isinstance(children, list) or any(str(child) not in self.nodes for child in children):
                    raise ValueError(f"curriculum_order.json: неизвестный узел или неверный список для {parent}")
                self.curriculum_order[parent] = [str(child) for child in children]

        mapping_file = path.parent / "relation_mapping.json"
        if relation_mapping is None:
            relation_mapping = json.loads(mapping_file.read_text(encoding="utf-8")) if mapping_file.exists() else {}
        if not isinstance(relation_mapping, dict):
            raise ValueError("relation_mapping.json: ожидается объект {тип: роль}.")
        available = {r["name"] for r in self.relations.values()}
        if any(v not in (*TEACHING_EDGES.keys(), "same_as", "ignore") for v in relation_mapping.values()):
            raise ValueError("Неверная учебная роль в relation_mapping.json.")
        # Старая настройка для удалённого/переименованного типа не должна
        # блокировать загрузку новой версии онтологии.
        self.relation_mapping = {k: v for k, v in relation_mapping.items() if k in available}

        self.search.attach_relations(self.relations.values())
        self.outgoing = defaultdict(list)
        self.incoming = defaultdict(list)
        self.educational_children = defaultdict(list)
        for r in self.relations.values():
            src, dst = r["source_node_id"], r["destination_node_id"]
            self.outgoing[src].append(r)
            self.incoming[dst].append(r)
            rule = TEACHING_EDGES.get(self.relation_kind(r))
            if rule:
                orientation, priority = rule
                parent, child = (src, dst) if orientation == "forward" else (dst, src)
                if parent != child:
                    self.educational_children[parent].append((child, r, priority))

    def semantic_triples(self, node_id: str | None = None, depth: int = 1,
                         limit: int = 250) -> list[dict]:
        """Подлежащий — предикат — объект из любой загруженной онтологии."""
        return OntologyReasoner(self).triples(node_id, depth, limit)

    def graph_distance(self, start: str, end: str, max_depth: int = 4) -> int | None:
        return self.search.context_distances(start, max_depth).get(end)

    def relation_kind(self, relation: dict) -> str:
        """Сопоставляем название связи выбранной онтологии с понятной программе ролью."""
        name = relation["name"]
        return self.relation_mapping.get(
            name, RELATION_ALIASES.get(name, name if name in (*TEACHING_EDGES.keys(), "same_as") else "ignore")
        )

    def relation_types(self) -> list[dict]:
        counts = defaultdict(int)
        for relation in self.relations.values():
            counts[relation["name"]] += 1
        return [
            {"name": name, "count": count, "role": self.relation_kind({"name": name})}
            for name, count in sorted(counts.items(), key=lambda item: item[0])
        ]

    def ordered_children(self, parent: str):
        """Сначала curriculum_order.json, затем типом связи и ID."""
        explicit = self.curriculum_order.get(parent, [])
        ranking = {node_id: i for i, node_id in enumerate(explicit)}
        entries = self.educational_children[parent]
        result = sorted(entries, key=lambda item: (
            0 if item[0] in ranking else 1,
            ranking.get(item[0], 999999),
            item[2],
            (0, int(item[0])) if item[0].isdigit() else (1, item[0]),
            (0, int(item[1]["id"])) if item[1]["id"].isdigit() else (1, item[1]["id"]),
        ))
        seen = set()
        for child, relation, _ in result:
            if child not in seen:
                seen.add(child)
                yield child, relation

    def reachable_region(self, roots: list[str], max_depth: int = 0) -> set[str]:
        """Область занятия по любым реальным рёбрам, включая входящие.

        0 означает всю связную компоненту каждого выбранного корня. Типы
        отношений влияют на объяснение, но не запрещают перемещение по графу.
        """
        from collections import deque

        distances = {root: 0 for root in roots if root in self.nodes}
        queue = deque(distances)
        while queue:
            nid = queue.popleft()
            if max_depth and distances[nid] >= max_depth:
                continue
            for relation in self.outgoing[nid] + self.incoming[nid]:
                other = (relation['destination_node_id']
                         if relation['source_node_id'] == nid
                         else relation['source_node_id'])
                if other not in distances:
                    distances[other] = distances[nid] + 1
                    queue.append(other)
        return set(distances)

    def adjacent_concepts(self, node_id: str,
                          included: set[str] | None = None) -> list[dict]:
        """Все соседние понятия без ограничения DFS или направления стрелки.

        Повторные отношения между двумя узлами объединены в одной подсказке,
        но информация об этих отношениях не теряется.
        """
        if node_id not in self.nodes:
            raise KeyError(node_id)
        grouped: dict[str, dict] = {}
        seen_relations: set[str] = set()
        for relation in self.outgoing[node_id] + self.incoming[node_id]:
            if relation['id'] in seen_relations:
                continue
            seen_relations.add(relation['id'])
            outgoing = relation['source_node_id'] == node_id
            other = (relation['destination_node_id'] if outgoing
                     else relation['source_node_id'])
            if included is not None and other not in included:
                continue
            entry = grouped.setdefault(other, {
                'id': other, 'name': self.nodes[other]['name'], 'relations': []
            })
            entry['relations'].append({
                'id': relation['id'], 'type': relation['name'],
                'role': self.relation_kind(relation),
                'direction': 'out' if outgoing else 'in',
            })
        return sorted(grouped.values(), key=lambda item: (
            item['name'].casefold(), str(item['id'])
        ))

    def learning_path(self, roots: list[str]) -> list[Step]:
        """Итеративный DFS: сначала вся ветка, потом следующая ветка."""
        visited = set()
        path: list[Step] = []
        for root in roots:
            stack = [Step(root)]
            while stack:
                step = stack.pop()
                if step.node_id in visited:
                    continue
                visited.add(step.node_id)
                path.append(step)
                children = list(self.ordered_children(step.node_id))
                for child, relation in reversed(children):
                    if child not in visited:
                        stack.append(Step(child, relation["id"], relation["name"], step.node_id))
        return path

    def neighborhood(self, node_id: str, types: set[str], direction: str):
        """Вернуть (соседний узел, связь), не теряя исходное направление."""
        out = []
        if direction in ("out", "both"):
            for r in self.outgoing[node_id]:
                if self.relation_kind(r) in types:
                    out.append((r["destination_node_id"], r))
        if direction in ("in", "both"):
            for r in self.incoming[node_id]:
                if self.relation_kind(r) in types:
                    out.append((r["source_node_id"], r))
        return out

    def matched_node(self, question: str, current_id: str | None = None,
                     preferred_ids: set[str] | None = None) -> str | None:
        """Независимое от тематики распознавание. Слабый поиск не засчитывает вопрос."""
        selected, _ = self.search.resolve(question, current_id, preferred_ids)
        return selected

    def is_contextual_question(self, question: str, current_id: str) -> bool:
        """Без явно названного понятия допускаются вопросы о ТЕКУЩЕЙ теме.

        Если есть посторонние содержательные слова, лучше признать вопрос
        нераспознанным, чем выдать определение нерелевантного узла.
        """
        terms = [word_stem(w) for w in normalize(question).split()]
        grammar = {word_stem(w) for w in (
            "что", "такое", "это", "какой", "какие", "каков", "где", "когда", "зачем",
            "почему", "как", "про", "из", "чего", "для", "чем", "о", "об", "с", "у",
            "есть", "бывают", "включает", "состоит", "используется", "применяется",
            "включены", "связи", "связано", "связаны", "понятие", "тема", "разновидности",
            "виды", "части", "компоненты", "какая", "назначение", "расскажи", "объясни",
            "опиши", "назови", "входные", "выходные", "данные", "результат", "имеет",
            "работает", "решает", "задачи", "помогает", "применить", "пример", "на практике", "применяют", "он", "она", "они", "его", "ее", "этого", "этой", "этот",
        )}
        stemmed_name = {word_stem(w) for w in normalize(self.nodes[current_id]["name"]).split()}
        content = [t for t in terms if len(t) >= 3 and t not in grammar and t not in stemmed_name]
        return not content

    def describe_relation(self, r: dict) -> dict:
        return {
            "id": r["id"],
            "type": r["name"] or "(без типа)",
            "role": self.relation_kind(r),
            "source_id": r["source_node_id"],
            "source": self.nodes[r["source_node_id"]]["name"],
            "destination_id": r["destination_node_id"],
            "destination": self.nodes[r["destination_node_id"]]["name"],
        }

    def matched_concepts(self, question: str, preferred_ids: set[str] | None = None,
                         limit: int = 4, include_weak: bool = False,
                         current_id: str | None = None) -> list[dict]:
        """Кандидаты: используются и для сопоставления, и для уточняющих подсказок."""
        matches = self.search.lookup(question, preferred_ids=preferred_ids,
                                     limit=max(limit + 5, 10), current_id=current_id)
        result, seen = [], set()
        for candidate in matches:
            if candidate.score < (.2 if include_weak else .76):
                continue
            key = normalize_query(candidate.name)
            if key in seen:
                continue
            seen.add(key)
            result.append({"id": candidate.id, "name": candidate.name,
                           "score": round(candidate.score, 3), "method": candidate.origin})
            if len(result) >= limit:
                break
        return result

    def subgraph(self, node_id: str, depth: int | str = 1, limit: int | None = None) -> dict:
        """1/2/3 уровня или ВСЕ узлы и связи (включая несвязанные компоненты)."""
        if node_id not in self.nodes:
            raise KeyError(node_id)
        if depth not in (1, 2, 3, 'all'):
            raise ValueError('Допустимы уровни 1, 2, 3 или all.')
        visited = {node_id}
        levels = {node_id: 0}
        if depth == 'all':
            visited = set(self.nodes)
            levels = {nid: 3 for nid in visited}
            levels[node_id] = 0
        else:
            frontier = {node_id}
            for level in range(1, depth + 1):
                following = set()
                for nid in frontier:
                    for r in self.outgoing[nid] + self.incoming[nid]:
                        other = r['destination_node_id'] if r['source_node_id'] == nid else r['source_node_id']
                        if other not in visited:
                            following.add(other)
                for nid in following:
                    levels.setdefault(nid, level)
                visited.update(following)
                frontier = following
                if not frontier:
                    break
        edge_items = [r for r in self.relations.values()
                      if r['source_node_id'] in visited and r['destination_node_id'] in visited]
        return {
            'center_id': node_id,
            'depth': depth,
            'nodes': [{'id': nid, 'name': self.nodes[nid]['name'], 'level': levels.get(nid, 3),
                       'definition': self.nodes[nid]['attributes'].get('def', '')}
                      for nid in sorted(visited)],
            'edges': [self.describe_relation(r) for r in edge_items],
            'truncated': False,
        }

    def quality_report(self) -> dict:
        from collections import Counter
        names = Counter(normalize_query(n["name"]) for n in self.nodes.values())
        duplicates = [n["name"] for n in self.nodes.values() if names[normalize_query(n["name"])] > 1]
        empty = [n["name"] for n in self.nodes.values() if not n["attributes"].get("def")]
        unnamed = [r["id"] for r in self.relations.values() if not r["name"].strip()]
        unmapped = [r["id"] for r in self.relations.values() if self.relation_kind(r) == "ignore"]
        # Простая проверка циклов в «является видом» (не полноценный OWL-reasoner).
        parents = defaultdict(list)
        for r in self.relations.values():
            if self.relation_kind(r) == "is_a":
                parents[r["source_node_id"]].append(r["destination_node_id"])
        cycles = []
        checked = set()
        def visit(nid, path):
            if nid in path:
                cycles.append(path[path.index(nid):] + [nid]); return
            if nid in checked:
                return
            checked.add(nid)
            for parent in parents[nid]:
                visit(parent, path + [nid])
        for nid in self.nodes:
            visit(nid, [])
        total = len(self.nodes)
        return {"nodes": total, "relations": len(self.relations),
                "definitions_count": self.definitions_count,
                "definition_coverage": round(self.definitions_count / total, 3),
                "missing_definitions": empty,
                "duplicate_names": sorted(set(duplicates)),
                "unnamed_relation_ids": unnamed,
                "unnamed_relation_details": [self.describe_relation(r) for r in self.relations.values()
                                             if not r["name"].strip()],
                "unmapped_relation_ids": unmapped,
                "is_a_cycles": cycles,
                "note": "Структурные проверки; логическая непротиворечивость формальных аксиом отдельно не доказана."}

    @staticmethod
    def definition_text(node: dict) -> str:
        """Не дублировать «Нейрон — Нейрон - это ...» в учебном ответе."""
        definition = str(node["attributes"].get("def", "")).strip()
        if not definition:
            return f"В онтологии пока не заполнено определение понятия «{node['name']}»."
        prefix = rf"^\s*{re.escape(node['name'])}\s*[-—:]\s*(?:это\s+)?"
        shorter = re.sub(prefix, "", definition, count=1, flags=re.IGNORECASE)
        if shorter != definition:
            definition = shorter
        elif definition.casefold().startswith('это '):
            definition = definition[4:]
        return definition

    def response(self, node_id: str, question: str) -> tuple[str, list[dict]]:
        """Вопрос -> определение -> объяснение по направленным семантическим тройкам."""
        reasoner = OntologyReasoner(self)
        other_id = None
        if detect_intent(question) == 'compare':
            # Берём реально упомянутый в запросе второй термин, а не
            # случайное текстовое сходство с корневым понятием.
            matches = self.matched_concepts(question, limit=8)
            for candidate in matches:
                if candidate['id'] != node_id and normalize_query(candidate['name']) != normalize_query(self.nodes[node_id]['name']):
                    other_id = candidate['id']
                    break
        response, sources, _ = reasoner.explain(node_id, question, other_id)
        return response, sources

    def suggested_questions(self, node_id: str) -> list[str]:
        name = self.nodes[node_id]["name"]
        choices = [f"Что такое {name}?"]
        if self.neighborhood(node_id, {"has"}, "out") or self.neighborhood(node_id, {"a_part_of"}, "in"):
            choices.append(f"Из чего состоит {name}?")
        if self.neighborhood(node_id, {"used_for", "use"}, "out"):
            choices.append(f"Для чего используется {name}?")
        if self.neighborhood(node_id, {"is_a", "is_instance"}, "in"):
            choices.append(f"Какие виды есть у понятия «{name}»?")
        choices.append(f"Какие связи есть у понятия «{name}»?")
        return choices[:4]

    def available_topics(self) -> list[dict]:
        result = []
        for nid, node in sorted(self.nodes.items(), key=lambda x: (0, int(x[0])) if x[0].isdigit() else (1, x[0])):
            result.append({"id": nid, "name": node["name"], "descendants": len(self.learning_path([nid]))})
        return result
