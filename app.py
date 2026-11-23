"""FastAPI: независимые онтологии и свободная навигация по графу во время занятия."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
from threading import RLock
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from graph import TEACHING_EDGES, Ontology, Step
from question_processing import detect_intent

BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = BASE_DIR / "data" / "ontologies"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
DEFAULT_ID = "default"

app = FastAPI(title="OntoTutor — обучающая система на графе знаний", version="8.0.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
lock = RLock()


@dataclass
class OntologyEntry:
    id: str
    title: str
    filename: str
    path: Path
    graph: Ontology


@dataclass
class Session:
    ontology_id: str
    ontology_title: str
    graph: Ontology  # ссылка на неизменный граф; смена настроек другого ученика не влияет на текущую сессию
    selected_topics: list[str]
    quota: int
    allowed_nodes: set[str]  # отмеченные темы и все доступные через них подтемы
    path: list[Step] = field(default_factory=list)  # фактическая история переходов пользователя
    position: int = 0  # индекс последнего вопроса (совместимость со статусом)
    visited_nodes: set[str] = field(default_factory=set)  # уже изученные понятия
    branch_number: int = 0
    branches_completed: int = 0
    answered: int = 0
    active: bool = True
    reason: str | None = None
    history: list[dict] = field(default_factory=list)  # сообщения вопрос/ответ, доступные для восстановления чата
    search_depth: int = 0  # 0 = весь доступный учебный граф
    question_category: str = 'any'  # разрешённый вид вопросов


# Исходная онтология сохраняется для совместимости с ранее созданным проектом.
ontology = Ontology(BASE_DIR / "data" / "ontology.ont")
ontologies: dict[str, OntologyEntry] = {
    DEFAULT_ID: OntologyEntry(DEFAULT_ID, "Исходная онтология", "ontology.ont", BASE_DIR / "data" / "ontology.ont", ontology)
}
sessions: dict[str, Session] = {}


def load_uploaded_ontologies() -> None:
    """Восстановление списка загруженных файлов после перезапуска FastAPI."""
    if not UPLOADS_DIR.exists():
        return
    for folder in sorted(UPLOADS_DIR.iterdir()):
        if not folder.is_dir() or not re.fullmatch(r"[0-9a-f]{32}", folder.name):
            continue
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
            path = folder / "ontology.ont"
            graph = Ontology(path)
            ontologies[folder.name] = OntologyEntry(folder.name, meta["title"], meta["filename"], path, graph)
        except (OSError, KeyError, ValueError, TypeError):
            # Неисправная отдельная загрузка не должна останавливать всё приложение.
            continue


load_uploaded_ontologies()


class CreateSession(BaseModel):
    ontology_id: str = DEFAULT_ID
    selected_topics: list[str] = Field(min_length=1, max_length=1000)
    question_count: int = Field(ge=1, le=100)
    search_depth: int = Field(default=0, ge=0, le=10)
    question_category: str = Field(default='any', pattern='^(any|definition|purpose|composition|types|relations|operation|compare|input|output)$')


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


class RelationMappingRequest(BaseModel):
    mapping: dict[str, str]


def entry_or_404(ontology_id: str) -> OntologyEntry:
    entry = ontologies.get(ontology_id)
    if entry is None:
        raise HTTPException(404, "Онтология не найдена. Выберите файл из списка.")
    return entry


def info(entry: OntologyEntry) -> dict:
    g = entry.graph
    return {
        "id": entry.id,
        "title": entry.title,
        "filename": entry.filename,
        "nodes": len(g.nodes),
        "relations": len(g.relations),
        "definitions": g.definitions_count,
        "definition_coverage": round(g.definitions_count / len(g.nodes), 3),
        "unmapped_relations": sum(t["count"] for t in g.relation_types() if t["role"] == "ignore"),
    }


def snapshot(sid: str, session: Session) -> dict:
    """Состояние занятия: последнее изученное понятие и ВСЕ его соседи.

    DFS служит для учебных рекомендаций, но не управляет допустимым
    порядком вопросов: пользователь выбирает следующий узел самостоятельно.
    """
    graph = session.graph
    focus = session.path[-1].node_id if session.path else None
    choosing = focus is None
    neighbors = (graph.adjacent_concepts(focus, session.allowed_nodes)
                 if focus else [])
    # Новые соседние понятия удобнее показывать первыми; назад по уже изученным
    # узлам вернуться также можно в любой момент.
    neighbors.sort(key=lambda entry: (
        entry['id'] in session.visited_nodes,
        entry['name'].casefold(), entry['id']
    ))
    if focus:
        suggestions = [f"Что такое {entry['name']}?" for entry in neighbors[:6]]
        # Можно уточнить вопрос о том же понятии, не переходя по графу.
        suggestions.extend(graph.suggested_questions(focus)[1:3])
    else:
        starters = list(dict.fromkeys(session.selected_topics))
        suggestions = [f"Что такое {graph.nodes[nid]['name']}?" for nid in starters[:5]]
    trace = []
    for step in session.path[-5:]:
        entry = {'node_id': step.node_id, 'name': graph.nodes[step.node_id]['name']}
        if step.via_relation_id:
            entry.update(relation_id=step.via_relation_id,
                         relation_type=step.via_relation_type)
        trace.append(entry)
    total = len(session.allowed_nodes)
    return {
        'session_id': sid,
        'ontology_id': session.ontology_id,
        'ontology_title': session.ontology_title,
        'active': session.active,
        'reason': session.reason,
        'question_count': session.quota,
        'questions_answered': session.answered,
        'remaining': max(0, session.quota - session.answered),
        'can_finish': session.active and session.answered >= session.quota,
        'mode': 'choose_start' if choosing else 'in_branch',
        'allowed_count': total,
        'explored_count': len(session.visited_nodes),
        'branch_number': session.branch_number,
        'branches_completed': session.branches_completed,
        'course_length': total if focus else 0,
        'current_index': max(0, len(session.visited_nodes) - 1) if focus else None,
        'branch_answered': session.answered,
        'selected_topics': session.selected_topics,
        'current': ({'id': focus, 'name': graph.nodes[focus]['name'],
                     'definition': graph.nodes[focus]['attributes'].get('def', '')}
                    if focus else None),
        'next': [{'id': item['id'], 'name': item['name']}
                 for item in neighbors],
        'neighbors': neighbors,
        'neighbor_count': len(neighbors),
        'trace': trace,
        'suggestions': list(dict.fromkeys(suggestions)),
        'review': focus is not None and len(session.visited_nodes) == total,
        'search_depth': session.search_depth,
        'question_category': session.question_category,
    }


@app.get("/")
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/ontologies")
def list_ontologies():
    with lock:
        return {"items": [info(entry) for entry in ontologies.values()], "default_id": DEFAULT_ID, "max_upload_mb": 5}


@app.post("/api/ontologies", status_code=201)
async def upload_ontology(file: UploadFile = File(...)):
    filename = (file.filename or "").replace("\\", "/").split("/")[-1]
    if Path(filename).suffix.lower() not in (".ont", ".json"):
        raise HTTPException(422, "Выберите .ont или .json (JSON-экспорт из ONTOLIS).")
    payload = await file.read(MAX_UPLOAD_BYTES + 1)
    await file.close()
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Файл слишком большой: максимальный размер — 5 МБ.")
    if not payload:
        raise HTTPException(422, "Файл пустой.")
    title = (Path(filename).stem.strip() or "Новая онтология")[:80]
    oid = uuid4().hex
    folder = UPLOADS_DIR / oid
    with lock:
        if len(ontologies) >= 51:
            raise HTTPException(409, "Достигнут предел в 50 загруженных онтологий.")
        folder.mkdir(parents=True, exist_ok=False)
        try:
            path = folder / "ontology.ont"
            path.write_bytes(payload)
            # У новых предметных областей свой пустой учебный порядок и словарь отношений.
            (folder / "curriculum_order.json").write_text("{}\n", encoding="utf-8")
            (folder / "relation_mapping.json").write_text("{}\n", encoding="utf-8")
            graph = Ontology(path)
            meta = {"title": title, "filename": filename}
            (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            entry = OntologyEntry(oid, title, filename, path, graph)
            ontologies[oid] = entry
        except (ValueError, UnicodeError, OSError) as exc:
            shutil.rmtree(folder, ignore_errors=True)
            if isinstance(exc, OSError):
                raise HTTPException(500, "Не удалось сохранить онтологию на диск.") from exc
            raise HTTPException(422, f"Не удалось прочитать онтологию: {exc}") from exc
    return info(entry)


@app.get("/api/ontologies/{ontology_id}/relations")
def get_relations(ontology_id: str):
    with lock:
        entry = entry_or_404(ontology_id)
        return {"ontology_id": ontology_id, "types": entry.graph.relation_types(), "roles": list(TEACHING_EDGES) + ["same_as", "ignore"]}


@app.put("/api/ontologies/{ontology_id}/relations")
def set_relations(ontology_id: str, body: RelationMappingRequest):
    """Настройка трактовки нестандартных отношений без изменения оригинального .ont."""
    with lock:
        entry = entry_or_404(ontology_id)
        possible = {r["name"] for r in entry.graph.relations.values()}
        allowed = set(TEACHING_EDGES) | {"same_as", "ignore"}
        if set(body.mapping) - possible or any(role not in allowed for role in body.mapping.values()):
            raise HTTPException(422, "Неизвестный тип связи или выбранная учебная роль.")
        try:
            replacement = Ontology(entry.path, relation_mapping=body.mapping)
            mapping_file = entry.path.parent / "relation_mapping.json"
            temporary = mapping_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(body.mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, mapping_file)
        except (OSError, ValueError) as exc:
            raise HTTPException(422, f"Не удалось сохранить настройки связей: {exc}") from exc
        # Старые сессии продолжат работать со старым объектом graph.
        entry.graph = replacement
        return {"ontology": info(entry), "types": replacement.relation_types()}


@app.get("/api/meta")
def meta(ontology_id: str = Query(DEFAULT_ID)):
    with lock:
        entry = entry_or_404(ontology_id)
        g = entry.graph
        topics = g.available_topics()
        default_selected = [max(topics, key=lambda t: t["descendants"])["id"]]
        return {
            "ontology_id": entry.id,
            "ontology_title": entry.title,
            "nodes": len(g.nodes),
            "relations": len(g.relations),
            "definitions": g.definitions_count,
            "topics": topics,
            "default_selected": default_selected,
            "question_limit": 100,
        }


@app.get("/api/ontologies/{ontology_id}/quality")
def ontology_quality(ontology_id: str):
    with lock:
        return entry_or_404(ontology_id).graph.quality_report()


@app.get("/api/ontologies/{ontology_id}/graph/{node_id}")
def ontology_subgraph(ontology_id: str, node_id: str, depth: str = Query("1")):
    with lock:
        graph = entry_or_404(ontology_id).graph
        if node_id not in graph.nodes:
            raise HTTPException(404, "Понятие не найдено в выбранной онтологии.")
        if depth not in ('1', '2', '3', 'all'):
            raise HTTPException(422, 'Выберите 1, 2, 3 уровня или «Все».')
        return graph.subgraph(node_id, depth if depth == 'all' else int(depth))


@app.get("/api/ontologies/{ontology_id}/triples")
def ontology_triples(ontology_id: str, node_id: str | None = None,
                     depth: int = Query(1, ge=1, le=3),
                     limit: int = Query(250, ge=1, le=5000)):
    """Направленные семантические тройки текущей загруженной онтологии."""
    with lock:
        graph = entry_or_404(ontology_id).graph
        if node_id is not None and node_id not in graph.nodes:
            raise HTTPException(404, "Понятие не найдено в выбранной онтологии.")
        triples = graph.semantic_triples(node_id, depth, limit)
        return {"ontology_id": ontology_id, "center_id": node_id,
                "items": triples, "count": len(triples)}


@app.get("/api/ontologies/{ontology_id}/search")
def search_ontology(ontology_id: str, q: str = Query(..., min_length=2, max_length=1000)):
    """Незачётный поиск: варианты ответа и степень сходства для уточнения."""
    with lock:
        g = entry_or_404(ontology_id).graph
        ident, candidates = g.search.resolve(q)
        return {"matched_id": ident, "candidates": [
            {"id": v.id, "name": v.name, "score": round(v.score, 3), "method": v.origin}
            for v in candidates[:5]]}


@app.post("/api/sessions")
def new_session(payload: CreateSession):
    with lock:
        entry = entry_or_404(payload.ontology_id)
        g = entry.graph
        roots = list(dict.fromkeys(payload.selected_topics))
        missing = [node_id for node_id in roots if node_id not in g.nodes]
        if missing:
            raise HTTPException(422, f"В выбранной онтологии нет узлов с ID: {', '.join(missing)}")
        # В отличие от прежнего DFS-фильтра, область включает родительские,
        # дочерние и боковые узлы независимо от направления отношения.
        # Пользовательский лимит глубины при этом сохраняется.
        allowed = g.reachable_region(roots, payload.search_depth)
        sid = uuid4().hex
        # Первое понятие выбирается вопросом, затем переходы свободны внутри области.
        s = Session(ontology_id=entry.id, ontology_title=entry.title, graph=g,
                    selected_topics=roots, quota=payload.question_count, allowed_nodes=allowed,
                    search_depth=payload.search_depth, question_category=payload.question_category)
        sessions[sid] = s
        return snapshot(sid, s)


@app.get("/api/sessions/{sid}")
def get_session(sid: str):
    with lock:
        s = sessions.get(sid)
        if not s:
            raise HTTPException(404, "Сессия не найдена")
        return snapshot(sid, s)


@app.get("/api/sessions/{sid}/history")
def session_history(sid: str):
    with lock:
        s = sessions.get(sid)
        if not s:
            raise HTTPException(404, "Сессия не найдена.")
        return {"session_id": sid, "items": list(s.history)}


def remember(s: Session, question: str, answer: str, result: str,
             sources: list[dict] | None = None, node_id: str | None = None,
             traversed_via: dict | None = None) -> None:
    s.history.extend((
        {"role": "user", "content": question},
        {"role": "bot", "content": answer, "result": result,
         "sources": sources or [], "traversed_via": traversed_via,
         "answered_node": (
             {"id": node_id, "name": s.graph.nodes[node_id]["name"]} if node_id else None)},
    ))
    if len(s.history) > 300:
        del s.history[:len(s.history) - 300]


@app.post("/api/sessions/{sid}/ask")
def ask(sid: str, body: AskRequest):
    with lock:
        session = sessions.get(sid)
        if session is None:
            raise HTTPException(404, 'Сессия не найдена')
        if not session.active:
            raise HTTPException(409, 'Сессия завершена')
        graph = session.graph
        question = body.question.strip()
        if len(question) < 2:
            raise HTTPException(422, 'Введите вопрос')
        focus = session.path[-1].node_id if session.path else None
        intent = detect_intent(question)
        if session.question_category != 'any' and intent != session.question_category:
            answer = (f'В настройках выбрана категория вопросов «{session.question_category}». '
                      'Сформулируйте вопрос этой категории или начните новое занятие. '
                      'Этот вопрос не засчитан.')
            remember(session, question, answer, 'unrecognized')
            return {'result': 'unrecognized', 'answer': answer, 'sources': [],
                    'state': snapshot(sid, session)}

        matched = graph.matched_node(question, focus,
                                     preferred_ids=session.allowed_nodes)
        if not ('?' in question or re.match(
            r'(?i)^\s*(что|какая|какие|какой|как|из чего|зачем|почему|где|для чего|чем|сколько|расскажи|объясни|назови|можно|имеет|состоит|опиши|сравни|поясни|подскажи)\b',
            question) or matched):
            raise HTTPException(422, 'Напишите вопрос или название понятия.')

        # Контекстное уточнение без имени («А для чего его используют?»)
        # относится к последнему ИЗУЧЕННОМУ понятию, а не к узлу, который
        # DFS когда-то поставил следующим.
        if matched is None and focus and graph.is_contextual_question(question, focus):
            matched = focus
        if matched is None:
            variants = graph.matched_concepts(question, session.allowed_nodes,
                                              limit=5, include_weak=True,
                                              current_id=focus)
            names = [v['name'] for v in variants]
            answer = ('Не удалось уверенно распознать понятие. '
                      + ('Возможно, вы имели в виду: '
                         + '; '.join(f'«{name}»' for name in names) + '. '
                         if names else 'Уточните название понятия. ')
                      + 'Вопрос не засчитан.')
            remember(session, question, answer, 'unrecognized')
            return {'result': 'unrecognized', 'answer': answer,
                    'alternatives': variants, 'sources': [],
                    'state': snapshot(sid, session)}
        if matched not in session.allowed_nodes:
            outside_answer, sources = graph.response(matched, question)
            answer = ('Понятие найдено, но находится за пределами выбранной '
                      'области или глубины занятия. Ниже справочный ответ '
                      '(без зачёта).\n\n' + outside_answer
                      + '\n\nЧтобы изучать его в этом занятии, начните новую '
                      'сессию с более широкой темой или глубиной поиска.')
            remember(session, question, answer, 'out_of_scope', sources, matched)
            return {'result': 'out_of_scope', 'answer': answer,
                    'sources': sources,
                    'answered_node': {'id': matched,
                                      'name': graph.nodes[matched]['name']},
                    'state': snapshot(sid, session)}

        # Пользователь МОЖЕТ задавать вопросы о любом доступном понятии.
        # Соседство определяет подпись перехода, но не его допустимость.
        transition = 'start' if focus is None else 'follow_up'
        via = None
        rel = None
        if focus is not None and matched != focus:
            direct = [relation for relation in
                      graph.outgoing[focus] + graph.incoming[focus]
                      if (relation['destination_node_id'] if
                          relation['source_node_id'] == focus else
                          relation['source_node_id']) == matched]
            if direct:
                # Только фактически существующая стрелка может быть причиной
                # перехода; её семантическое направление не меняем.
                rel = direct[0]
                via = graph.describe_relation(rel)
                transition = 'neighbor'
            elif matched in session.visited_nodes:
                transition = 'return'
            else:
                transition = 'topic_change'
                session.branch_number += 1
        elif focus is None:
            session.branch_number = 1

        answer, sources = graph.response(matched, question)
        if transition == 'topic_change':
            answer += ('\n\nВы перешли к другой части выбранной области. '
                       'Можно также вернуться к предыдущему понятию.')
        session.path.append(Step(matched,
                                 via_relation_id=rel['id'] if rel else None,
                                 via_relation_type=rel['name'] if rel else None,
                                 parent_id=focus))
        session.position = len(session.path) - 1
        session.visited_nodes.add(matched)
        session.answered += 1
        finished = (len(session.visited_nodes) == len(session.allowed_nodes))
        if finished and not session.branches_completed:
            session.branches_completed = 1
        triples = graph.semantic_triples(matched, depth=1, limit=100)
        remember(session, question, answer, 'answered', sources, matched, via)
        return {
            'result': 'answered', 'answer': answer,
            'answered_node': {'id': matched,
                              'name': graph.nodes[matched]['name']},
            'traversed_via': via,
            'transition': transition,
            'branch_completed': finished,
            'sources': sources,
            'semantic_triples': triples,
            'state': snapshot(sid, session),
        }


@app.post("/api/sessions/{sid}/complete")
def complete(sid: str):
    with lock:
        s = sessions.get(sid)
        if not s:
            raise HTTPException(404, "Сессия не найдена")
        if not s.active:
            raise HTTPException(409, "Сессия уже завершена")
        if s.answered < s.quota:
            raise HTTPException(409, f"Нужно ещё {s.quota - s.answered} вопрос(ов), либо используйте принудительное завершение")
        s.active, s.reason = False, "completed"
        return snapshot(sid, s)


@app.post("/api/sessions/{sid}/force")
def force_end(sid: str):
    with lock:
        s = sessions.get(sid)
        if not s:
            raise HTTPException(404, "Сессия не найдена")
        if not s.active:
            raise HTTPException(409, "Сессия уже завершена")
        s.active, s.reason = False, "forced"
        return snapshot(sid, s)
