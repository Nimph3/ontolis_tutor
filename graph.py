"""Онтология ONTOLIS: индексы, семантические связи и учебный DFS-обход."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import re
import unicodedata
from question_processing import SYNONYMS, detect_intent, phrase_score, aliases, normalize as normalize_query


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

# Явный учебный порядок между дочерними понятиями, где он значим.
# В оригинальном ONTOLIS-представлении свойства 'order' нет.
DEFAULT_ORDER = {
    "3": ["46", "26", "27", "28", "29", "58"],
    "46": ["2"],
    "2": ["8", "10", "12", "14", "16", "20", "60"],
    "8": ["111", "112", "113"],
    "20": ["103", "105", "107", "109"],
    "49": ["63", "53", "65"],
    "63": ["64"],
    "185": ["187", "188", "191"],
    "112": ["185", "105", "109"],
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
            def_key = next((key for key in ("def", "Определение", "определение", "definition", "Definition", "Описание", "description")
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
        if any(k not in available or v not in (*TEACHING_EDGES.keys(), "same_as", "ignore") for k, v in relation_mapping.items()):
            raise ValueError("Неверный тип исходной связи или учебная роль в relation_mapping.json.")
        self.relation_mapping = dict(relation_mapping)

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

    def relation_kind(self, relation: dict) -> str:
        """Сопоставляем название связи выбранной онтологии с понятной программе ролью."""
        name = relation["name"]
        return self.relation_mapping.get(name, "used_for" if name == "use_for" else name if name in (*TEACHING_EDGES.keys(), "same_as") else "ignore")

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

    def matched_node(self, question: str, current_id: str | None = None, preferred_ids: set[str] | None = None) -> str | None:
        """Поиск по названиям. При равном совпадении предпочитаем доступный узел.

        current_id используется при прохождении ветки (например, два одноимённых
        понятия). В режиме выбора начала он отсутствует.
        """
        normalized = normalize(question)
        padded = f" {normalized} "

        def choose(ties: list[str]) -> str:
            if current_id in ties:
                return current_id
            inside = [nid for nid in ties if preferred_ids is not None and nid in preferred_ids]
            return sorted(inside or ties, key=lambda x: (0, int(x)) if x.isdigit() else (1, x))[0]
        exact_matches = []
        for nid, node in self.nodes.items():
            name = normalize(node["name"])
            if f" {name} " in padded:
                exact_matches.append((len(name.split()), len(name), nid))
        if exact_matches:
            longest = max((x[0], x[1]) for x in exact_matches)
            ties = [nid for wc, length, nid in exact_matches if (wc, length) == longest]
            return choose(ties)

        # Короткое обозначение лабораторной работы, например «ЛР5».
        lab = re.search(r"\bлр\s*([256])\b", normalized)
        if lab:
            shorthand = "лр" + lab.group(1)
            match = [nid for nid, n in self.nodes.items() if normalize(n["name"]).startswith(shorthand + " ")]
            if match:
                return choose(match)

        # Поддержка элементарных падежных изменений: «из чего состоит нейронная сеть».
        # Чужое понятие распознаём только при существенном совпадении слов.
        words = [word_stem(w) for w in normalized.split()]
        stop = {"что", "так", "это", "каков", "из", "каких", "чего", "как", "есть", "для", "чем", "можно", "какие", "какой", "состо", "где", "про", "расскажи", "объясни"}
        question_terms = {w for w in words if w not in stop and len(w) >= 4}
        candidates = []
        for nid, node in self.nodes.items():
            stems = [word_stem(w) for w in normalize(node["name"]).split()]
            relevant = [w for w in stems if len(w) >= 4]
            if not relevant:
                continue
            common = sum(w in question_terms for w in relevant)
            if common == len(relevant) or (len(relevant) > 1 and common >= 2 and common / len(relevant) >= 0.66):
                candidates.append((common / len(relevant), common, len(" ".join(relevant)), nid))
        if candidates:
            candidates.sort(reverse=True)
            best = candidates[0][:3]
            ties = [c[3] for c in candidates if c[:3] == best]
            if current_id in ties:
                return current_id
            return sorted(ties, key=lambda x: (0, int(x)) if x.isdigit() else (1, x))[0]
        # Контролируемый fallback: алиасы, синонимы и небольшие опечатки.
        matches = self.matched_concepts(question, preferred_ids=preferred_ids)
        if matches:
            best = matches[0]["score"]
            ties = [m["id"] for m in matches if m["score"] == best]
            return choose(ties)
        return None

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
                         limit: int = 4) -> list[dict]:
        """Список кандидатов с оценкой релевантности; исключает ненадёжные совпадения."""
        found = []
        q = normalize_query(question)
        for nid, node in self.nodes.items():
            terms = [node["name"]]
            for key in ("aliases", "synonyms", "Синонимы", "Алиасы", "Краткое название"):
                terms += aliases(node["attributes"].get(key))
            for short, fulls in SYNONYMS.items():
                if normalize_query(node["name"]) in [normalize_query(v) for v in fulls]:
                    terms.append(short)
            score = max(phrase_score(q, term) for term in terms)
            if score >= .7:
                found.append({"id": nid, "name": node["name"], "score": round(score, 2)})
        found.sort(key=lambda item: (-item["score"],
                   -(len(normalize_query(item["name"]).split())),
                   0 if preferred_ids and item["id"] in preferred_ids else 1,
                   item["id"]))
        # Не выдаём одинаковое название несколько раз без необходимости.
        unique = []
        seen_names = set()
        for item in found:
            name = normalize_query(item["name"])
            if name in seen_names:
                continue
            unique.append(item)
            seen_names.add(name)
            if len(unique) >= limit:
                break
        return unique

    def subgraph(self, node_id: str, depth: int = 1, limit: int = 13) -> dict:
        """Ограниченный подграф с направлениями рёбер для отдельного окна."""
        if node_id not in self.nodes:
            raise KeyError(node_id)
        depth = max(1, min(depth, 2))
        visited, frontier = {node_id}, {node_id}
        for _ in range(depth):
            next_layer = set()
            for nid in sorted(frontier):
                for neighbor, relation in self.neighborhood(
                    nid, set(TEACHING_EDGES) | {"same_as", "ignore"}, "both"
                ):
                    if neighbor not in visited and len(visited) + len(next_layer) < limit:
                        next_layer.add(neighbor)
            visited.update(next_layer)
            frontier = next_layer
            if not frontier:
                break
        # Нестандартные отношения тоже показываем: визуализация != учебный DFS.
        edge_items = [r for r in self.relations.values()
                      if r["source_node_id"] in visited and r["destination_node_id"] in visited]
        return {
            "center_id": node_id,
            "nodes": [{"id": nid, "name": self.nodes[nid]["name"],
                       "definition": self.nodes[nid]["attributes"].get("def", "")}
                      for nid in sorted(visited)],
            "edges": [self.describe_relation(r) for r in edge_items[:100]],
            "truncated": len(edge_items) > 100 or len(visited) >= limit,
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
        """Определения -> ответ на намерение -> связанные понятия, только из графа."""
        node = self.nodes[node_id]
        source = [{"node_id": node_id, "field": node["_definition_source"]}]
        definition = self.definition_text(node)
        intent = detect_intent(question)
        # Два явных понятия -> предварительно объясняем оба.
        matches = self.matched_concepts(question, limit=6)
        comparisons = [candidate for candidate in matches if candidate["id"] != node_id
                       and normalize_query(candidate["name"]) != normalize_query(node["name"])]
        parts = [f"Определение: {node['name']} — {definition}" if node["attributes"].get("def")
                 else definition]
        if intent == "compare" and comparisons:
            second_id = comparisons[0]["id"]
            second = self.nodes[second_id]
            second_def = self.definition_text(second)
            parts.append(f"Определение второго понятия: {second['name']} — {second_def}")
            source.append({"node_id": second_id, "field": second["_definition_source"]})
            connected = [r for r in self.outgoing[node_id] + self.incoming[node_id]
                         if second_id in (r["source_node_id"], r["destination_node_id"])]
            if connected:
                parts.append("Связь между понятиями: " + "; ".join(
                    f"{self.describe_relation(r)['source']} — {r['name'] or 'связано с'} → {self.describe_relation(r)['destination']}"
                    for r in connected[:3]) + ".")
                source.extend({"relation": self.describe_relation(r), "node_id": second_id, "field": "name"}
                              for r in connected[:3])
            else:
                parts.append("Прямое сравнительное отношение в графе не задано. Сравните определения выше: система не придумывает различия, отсутствующие в онтологии.")
            return "\n\n".join(parts), source

        kinds = {
            "composition": ("Состав", {"has"}, "out", {"a_part_of"}, "in"),
            "types": ("Виды и экземпляры", {"is_a", "is_instance"}, "in", set(), "out"),
            "purpose": ("Назначение и применение", {"used_for", "use"}, "out", set(), "out"),
            "input": ("Входные данные", {"input"}, "in", set(), "out"),
            "output": ("Выходные данные", {"output"}, "out", set(), "out"),
            "relations": ("Связи", set(TEACHING_EDGES) | {"same_as"}, "both", set(), "out"),
            "operation": ("Связанные механизмы", {"use", "has", "input", "output"}, "both", set(), "out"),
        }
        def add_relations(label, items):
            used = set()
            filtered = []
            for neighbor_id, relation in items:
                if relation["id"] not in used:
                    used.add(relation["id"])
                    filtered.append((neighbor_id, relation))
            if filtered:
                parts.append(label + ": " + "; ".join(
                    f"{self.nodes[nid]['name']} ({relation['name'] or 'связь'})"
                    for nid, relation in filtered[:8]) + ".")
                for nid, relation in filtered[:8]:
                    source.append({"relation": self.describe_relation(relation), "node_id": nid, "field": "name"})
        if intent in kinds:
            label, types, direction, types2, direction2 = kinds[intent]
            relevant = self.neighborhood(node_id, types, direction) + self.neighborhood(node_id, types2, direction2)
            # До объяснения связи определяем до трёх дополнительных терминов,
            # чтобы ответ был пригоден для обучения без предварительной подготовки.
            related_defs = []
            seen_definitions = set()
            for related_id, _relation in relevant:
                if related_id not in seen_definitions and related_id != node_id:
                    seen_definitions.add(related_id)
                    related_node = self.nodes[related_id]
                    if related_node["attributes"].get("def"):
                        related_defs.append(f"{related_node['name']} — {self.definition_text(related_node)}")
                        source.append({"node_id": related_id, "field": related_node["_definition_source"]})
                if len(related_defs) >= 3:
                    break
            if related_defs:
                parts.append("Определения связанных понятий:\n" + "\n".join(related_defs))
            add_relations(label, relevant)
        else:
            # Даже определение дополняется назначением и контекстом, но только из графа.
            applications = self.neighborhood(node_id, {"used_for", "use"}, "out")
            if applications:
                add_relations("Назначение и применение", applications[:4])
            related = self.neighborhood(node_id, set(TEACHING_EDGES), "both")
            if related:
                add_relations("Связанные понятия", related[:5])
        return "\n\n".join(parts), source

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
