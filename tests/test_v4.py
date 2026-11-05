"""Регрессионные проверки учебной логики, чтения ONTOLIS и новых API."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app import app, ontology, ontologies
from graph import Ontology
from question_processing import detect_intent

client = TestClient(app)


def test_russian_definition_field_is_read():
    source = next(e.graph for e in ontologies.values() if len(e.graph.nodes) == 50)
    assert source.definitions_count == 50
    assert "технология" in source.nodes["1"]["attributes"]["def"]
    answer, refs = source.response("1", "Что такое искусственный интеллект?")
    assert "технология" in answer
    assert refs[0]["field"] == "attributes.Определение"


def test_original_ontology_unchanged():
    assert len(ontology.nodes) == 54
    assert ontology.nodes["64"]["attributes"]["def"].startswith("Нейрон - это")


def test_synonym_and_flexible_formulations():
    for question in ("Что такое нейросеть?", "Объясни нейросети", "Расскажи про искусственные нейронные сети"):
        assert ontology.matched_node(question) == "49"
    assert ontology.matched_node("Для чего нужен нейрон?") == "64"
    assert ontology.matched_node("Почему ветер дует?") is None


def test_intent_recognition():
    examples = {
        "Чем отличается нейрон от нейросети?": "compare",
        "Из чего состоит нейронная сеть?": "composition",
        "Для чего нужен нейрон?": "purpose",
        "Какие виды существуют?": "types",
        "Как работает симулятор?": "operation",
        "Что такое нейрон?": "definition",
    }
    for sentence, expected in examples.items():
        assert detect_intent(sentence) == expected


def test_answer_adds_context_from_ontology():
    answer, sources = ontology.response("49", "Из чего состоит искусственная нейронная сеть?")
    assert "Определение:" in answer and "Состав:" in answer
    assert any(item.get("relation") for item in sources)


def test_comparison_has_two_definitions_and_no_hallucination():
    answer, sources = ontology.response("64", "Чем отличается нейрон от искусственной нейронной сети?")
    assert "Определение второго понятия:" in answer
    assert len([item for item in sources if item["field"].startswith("attributes")]) >= 2


def test_subgraph_is_bounded_and_direction_preserved():
    result = client.get("/api/ontologies/default/graph/49?depth=1")
    assert result.status_code == 200
    data = result.json()
    assert data["center_id"] == "49"
    assert len(data["nodes"]) >= 2
    assert all("source_id" in item and "destination_id" in item for item in data["edges"])
    assert client.get("/api/ontologies/default/graph/missing").status_code == 404
    assert client.get("/api/ontologies/default/graph/49?depth=99").status_code == 422


def test_quality_report_is_honest():
    data = client.get("/api/ontologies/default/quality").json()
    assert data["nodes"] == 54
    assert data["definitions_count"] == 54
    assert "Функция активации" in data["duplicate_names"]
    assert len(data["unnamed_relation_ids"]) == 2
    assert "логическая" in data["note"] or "логическую" in data["note"]


def test_history_restores_success_and_failed_questions():
    created = client.post("/api/sessions", json={"selected_topics": ["64"], "question_count": 1}).json()
    sid = created["session_id"]
    answered = client.post(f"/api/sessions/{sid}/ask", json={"question": "Объясни нейрон"}).json()
    assert answered["result"] == "answered"
    invalid = client.post(f"/api/sessions/{sid}/ask", json={"question": "Почему ветер дует?"}).json()
    assert invalid["result"] == "unrecognized"
    history = client.get(f"/api/sessions/{sid}/history").json()["items"]
    assert [item["role"] for item in history] == ["user", "bot", "user", "bot"]
    assert history[1]["result"] == "answered"
    assert history[3]["result"] == "unrecognized"
    assert invalid["state"]["questions_answered"] == 1


def test_user_can_enter_term_without_question_mark():
    s = client.post("/api/sessions", json={"selected_topics": ["49"], "question_count": 1}).json()
    result = client.post(f"/api/sessions/{s['session_id']}/ask", json={"question": "Нейросеть"})
    assert result.status_code == 200 and result.json()["result"] == "answered"


def test_ui_contains_help_graph_and_status():
    body = client.get("/").text
    for element in ('id="helpDialog"', 'id="graphDialog"', 'id="liveStatus"', 'id="qualityDialog"'):
        assert element in body


def test_uploaded_json_original_properties_are_not_overwritten(tmp_path: Path):
    source = tmp_path / "sample.ont"
    source.write_text(json.dumps({"nodes": [{"id": "n", "name": "Понятие", "attributes": {
        "Определение": "Проверка значения", "Категория": "Учебная"}}], "relations": []}, ensure_ascii=False), encoding="utf8")
    graph = Ontology(source)
    assert graph.nodes["n"]["attributes"]["Категория"] == "Учебная"
    assert graph.nodes["n"]["attributes"]["def"] == "Проверка значения"
    assert "def" not in json.loads(source.read_text(encoding="utf8"))["nodes"][0]["attributes"]
