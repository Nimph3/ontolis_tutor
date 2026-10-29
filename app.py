"""FastAPI: каталог независимых ONTOLIS-графов и закреплённые за ними учебные сессии."""
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

from graph import DEFAULT_ORDER, TEACHING_EDGES, Ontology, Step

BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = BASE_DIR / "data" / "ontologies"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
DEFAULT_ID = "default"

app = FastAPI(title="Онтологический тьютор", version="3.0.0")
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
    path: list[Step] = field(default_factory=list)  # маршрут ТОЛЬКО текущей ветки
    position: int = 0  # индекс следующего вопроса в текущей ветке
    branch_number: int = 0
    branches_completed: int = 0
    answered: int = 0
    active: bool = True
    reason: str | None = None
    history: list[dict] = field(default_factory=list)


# Исходная онтология сохраняется для совместимости с ранее созданным проектом.
ontology = Ontology(BASE_DIR / "data" / "ontology.ont", default_order=DEFAULT_ORDER)
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
        "unmapped_relations": sum(t["count"] for t in g.relation_types() if t["role"] == "ignore"),
    }


def snapshot(sid: str, session: Session) -> dict:
    """Состояние двух режимов: выбор начального узла и обязательный обход ветки."""
    g = session.graph
    choosing = not session.path or session.position >= len(session.path)
    step = None if choosing else session.path[session.position]
    trace = []
    if step:
        # Родители каждого шага сохраняются внутри текущего DFS-маршрута.
        lookup = {p.node_id: p for p in session.path[:session.position + 1]}
        cursor = step
        while cursor:
            item = {"node_id": cursor.node_id, "name": g.nodes[cursor.node_id]["name"]}
            if cursor.via_relation_id:
                item["relation_id"] = cursor.via_relation_id
                item["relation_type"] = cursor.via_relation_type
            trace.append(item)
            cursor = lookup.get(cursor.parent_id) if cursor.parent_id else None
        trace.reverse()

    # Рекомендации не ограничивают выбор: стартовать можно с ЛЮБОГО
    # понятия внутри выбранной области, в том числе из глубины ветки.
    examples = list(dict.fromkeys(session.selected_topics + [p.node_id for p in g.learning_path(session.selected_topics)]))
    suggestions = (g.suggested_questions(step.node_id) if step else
                   [f"Что такое {g.nodes[nid]['name']}?" for nid in examples[:3]])
    return {
        "session_id": sid,
        "ontology_id": session.ontology_id,
        "ontology_title": session.ontology_title,
        "active": session.active,
        "reason": session.reason,
        "question_count": session.quota,
        "questions_answered": session.answered,
        "remaining": max(0, session.quota - session.answered),
        "can_finish": session.active and session.answered >= session.quota,
        "mode": "choose_start" if choosing else "in_branch",
        "allowed_count": len(session.allowed_nodes),
        "branch_number": session.branch_number,
        "branches_completed": session.branches_completed,
        "course_length": len(session.path),  # длина последней/текущей ветки (НЕ всех выбранных тем)
        "current_index": session.position if step else None,
        "branch_answered": session.position,
        "selected_topics": session.selected_topics,
        "current": ({"id": step.node_id, "name": g.nodes[step.node_id]["name"],
                     "definition": g.nodes[step.node_id].get("attributes", {}).get("def", "")}
                    if step else None),
        "next": ([{"id": p.node_id, "name": g.nodes[p.node_id]["name"]}
                  for p in session.path[session.position + 1:session.position + 4]] if step else []),
        "trace": trace,
        "suggestions": suggestions,
        "review": choosing and session.branch_number > 0,
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
            replacement = Ontology(entry.path, default_order=DEFAULT_ORDER if ontology_id == DEFAULT_ID else None, relation_mapping=body.mapping)
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
        default_selected = ["3"] if ontology_id == DEFAULT_ID and "3" in g.nodes else [max(topics, key=lambda t: t["descendants"])["id"]]
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


@app.post("/api/sessions")
def new_session(payload: CreateSession):
    with lock:
        entry = entry_or_404(payload.ontology_id)
        g = entry.graph
        roots = list(dict.fromkeys(payload.selected_topics))
        missing = [node_id for node_id in roots if node_id not in g.nodes]
        if missing:
            raise HTTPException(422, f"В выбранной онтологии нет узлов с ID: {', '.join(missing)}")
        allowed = {step.node_id for step in g.learning_path(roots)}
        sid = uuid4().hex
        # Новый маршрут пока НЕ строим: пользователь задаст старт первым вопросом.
        s = Session(ontology_id=entry.id, ontology_title=entry.title, graph=g,
                    selected_topics=roots, quota=payload.question_count, allowed_nodes=allowed)
        sessions[sid] = s
        return snapshot(sid, s)


@app.get("/api/sessions/{sid}")
def get_session(sid: str):
    with lock:
        s = sessions.get(sid)
        if not s:
            raise HTTPException(404, "Сессия не найдена")
        return snapshot(sid, s)


@app.post("/api/sessions/{sid}/ask")
def ask(sid: str, body: AskRequest):
    with lock:
        s = sessions.get(sid)
        if not s:
            raise HTTPException(404, "Сессия не найдена")
        if not s.active:
            raise HTTPException(409, "Сессия завершена")
        g = s.graph
        question = body.question.strip()
        if len(question) < 2:
            raise HTTPException(422, "Введите вопрос")
        if not ("?" in question or re.match(r"(?i)^\s*(что|какая|какие|какой|как|из чего|зачем|почему|где|для чего|чем|сколько|расскажи|объясни|назови|можно|имеет|состоит|опиши)\b", question)):
            raise HTTPException(422, "Сформулируйте вопрос, например: «Что такое нейрон?»")
        choosing = not s.path or s.position >= len(s.path)
        current_id = None if choosing else s.path[s.position].node_id
        matched = g.matched_node(question, current_id, preferred_ids=s.allowed_nodes)

        if choosing:
            if matched is None:
                return {
                    "result": "unrecognized",
                    "answer": "Назовите понятие, с которого хотите начать. Можно выбрать любую отмеченную тему или её подтему. Например: «Что такое …?». Вопрос не засчитан.",
                    "sources": [], "state": snapshot(sid, s),
                }
            if matched not in s.allowed_nodes:
                return {
                    "result": "out_of_scope",
                    "answer": (f"Понятие «{g.nodes[matched]['name']}» не входит в выбранные темы и их подтемы. "
                               "Выберите понятие из доступной области. Для смены чекбоксов завершите сессию. Вопрос не засчитан."),
                    "sources": [], "state": snapshot(sid, s),
                }
            # Стартовая точка выбрана САМИМ вопросом. DFS вычисляем лишь для неё.
            s.path = [step for step in g.learning_path([matched]) if step.node_id in s.allowed_nodes]
            s.position = 0
            s.branch_number += 1
            current_id = matched
        else:
            if matched and matched != current_id:
                name = g.nodes[matched]["name"]
                current_name = g.nodes[current_id]["name"]
                return {
                    "result": "out_of_order",
                    "answer": (f"Вы спрашиваете о понятии «{name}», а сейчас по DFS изучаем «{current_name}». "
                               "Сначала пройдём оставшиеся понятия этой ветки, затем вы сможете "
                               "выбрать любую следующую точку. Вопрос не засчитан."),
                    "sources": [], "state": snapshot(sid, s),
                }
            if matched is None and not g.is_contextual_question(question, current_id):
                return {
                    "result": "unrecognized",
                    "answer": ("Не удалось сопоставить вопрос с текущим понятием. Назовите его полностью "
                               "или выберите предложенный вопрос. Вопрос не засчитан."),
                    "sources": [], "state": snapshot(sid, s),
                }

        step = s.path[s.position]
        answer, sources = g.response(current_id, question)
        s.history.append({"question": question, "node_id": current_id, "answer": answer})
        s.answered += 1
        s.position += 1
        finished_branch = s.position == len(s.path)
        if finished_branch:
            s.branches_completed += 1
        state = snapshot(sid, s)
        return {
            "result": "answered",
            "answer": answer,
            "answered_node": {"id": current_id, "name": g.nodes[current_id]["name"]},
            "traversed_via": g.describe_relation(g.relations[step.via_relation_id]) if step.via_relation_id else None,
            "branch_completed": finished_branch,
            "sources": sources,
            "state": state,
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
