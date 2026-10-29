"""Новые сценарии: загрузка, другие ID, трактовка связей, изоляция сессий."""
import json
import shutil

import pytest
from fastapi.testclient import TestClient

from app import UPLOADS_DIR, app, ontologies, sessions, load_uploaded_ontologies

client = TestClient(app)


def sample_graph(definitions=True):
    return {
        "nodes": [
            {"id": "a", "name": "Альфа", "attributes": {"def": "Альфа — начало."}},
            {"id": "b", "name": "Бета", "attributes": {"def": "Бета — продолжение."} if definitions else {}},
            {"id": "c", "name": "Гамма", "attributes": {"def": "Гамма — конец."}},
        ],
        "relations": [
            {"id": "r-1", "name": "contains", "source_node_id": "a", "destination_node_id": "b"},
            {"id": "r-2", "name": "has", "source_node_id": "b", "destination_node_id": "c"},
        ],
    }


@pytest.fixture
def upload():
    created = []

    def make(graph=None, filename="Учебная область.ont"):
        body = json.dumps(graph or sample_graph(), ensure_ascii=False).encode("utf-8")
        response = client.post("/api/ontologies", files={"file": (filename, body, "application/octet-stream")})
        assert response.status_code == 201, response.text
        oid = response.json()["id"]
        created.append(oid)
        return response.json()

    yield make
    for oid in created:
        ontologies.pop(oid, None)
        shutil.rmtree(UPLOADS_DIR / oid, ignore_errors=True)


def test_upload_and_independent_graphs(upload):
    result = upload()
    oid = result["id"]
    assert result["nodes"] == 3
    assert result["definitions"] == 3
    assert result["unmapped_relations"] == 1
    assert (UPLOADS_DIR / oid / "curriculum_order.json").read_text().strip() == "{}"
    assert (UPLOADS_DIR / oid / "relation_mapping.json").exists()
    assert oid in [x["id"] for x in client.get("/api/ontologies").json()["items"]]
    metadata = client.get(f"/api/meta?ontology_id={oid}").json()
    assert len(metadata["topics"]) == 3
    assert metadata["default_selected"] == ["b"]  # b -> c; contains ещё не сопоставлен
    assert client.get("/api/meta").json()["nodes"] == 54  # исходный граф не затронут


def test_relation_mapping_and_old_session_immutable(upload):
    oid = upload()["id"]
    create_body = {"ontology_id": oid, "selected_topics": ["a"], "question_count": 2}
    earlier = client.post("/api/sessions", json=create_body).json()
    assert earlier["course_length"] == 0  # ожидаем первого вопроса, маршрут ещё не выбран
    assert earlier["allowed_count"] == 1

    res = client.put(f"/api/ontologies/{oid}/relations", json={"mapping": {"contains": "has"}})
    assert res.status_code == 200, res.text
    assert res.json()["ontology"]["unmapped_relations"] == 0
    assert json.loads((UPLOADS_DIR / oid / "relation_mapping.json").read_text()) == {"contains": "has"}

    later = client.post("/api/sessions", json=create_body).json()
    assert later["course_length"] == 0
    assert later["allowed_count"] == 3
    assert earlier["allowed_count"] == 1
    assert client.get(f'/api/sessions/{earlier["session_id"]}').json()["allowed_count"] == 1
    first = client.post(f'/api/sessions/{later["session_id"]}/ask', json={"question": "Что такое Альфа?"}).json()
    assert first["state"]["course_length"] == 3
    assert first["result"] == "answered"
    assert "начало" in first["answer"]
    assert first["state"]["current"]["id"] == "b"
    second = client.post(f'/api/sessions/{later["session_id"]}/ask', json={"question": "Что такое Бета?"}).json()
    assert second["result"] == "answered"
    assert second["traversed_via"]["role"] == "has"
    assert second["traversed_via"]["type"] == "contains"
    assert second["state"]["current"]["id"] == "c"
    finished = client.post(f'/api/sessions/{later["session_id"]}/complete')
    assert finished.status_code == 200
    assert finished.json()["ontology_id"] == oid


def test_upload_restored_after_restart(upload):
    oid = upload()["id"]
    ontologies.pop(oid)
    load_uploaded_ontologies()
    assert oid in ontologies
    assert client.get(f"/api/meta?ontology_id={oid}").status_code == 200


def test_upload_malformed_or_wrong_extension_rejected():
    r = client.post("/api/ontologies", files={"file": ("bad.ont", b"not json", "application/octet-stream")})
    assert r.status_code == 422 and "JSON" in r.json()["detail"]
    r = client.post("/api/ontologies", files={"file": ("bad.xml", b"<xml/>", "application/octet-stream")})
    assert r.status_code == 422
    r = client.post("/api/ontologies", files={"file": ("large.ont", b"1" * (5 * 1024 * 1024 + 1), "application/octet-stream")})
    assert r.status_code == 413


def test_validation_duplicate_unknown_references_and_mapping(upload):
    graph = sample_graph()
    graph["nodes"][1]["id"] = "a"
    bad = client.post("/api/ontologies", files={"file": ("duplicate.ont", json.dumps(graph).encode(), "application/octet-stream")})
    assert bad.status_code == 422
    graph = sample_graph()
    graph["relations"][0]["destination_node_id"] = "missing"
    bad = client.post("/api/ontologies", files={"file": ("missing.ont", json.dumps(graph).encode(), "application/octet-stream")})
    assert bad.status_code == 422
    oid = upload()["id"]
    assert client.put(f"/api/ontologies/{oid}/relations", json={"mapping": {"contains": "nonsense"}}).status_code == 422
    assert client.put(f"/api/ontologies/{oid}/relations", json={"mapping": {"phantom": "has"}}).status_code == 422
    assert client.get("/api/meta?ontology_id=wrong").status_code == 404
    assert client.post("/api/sessions", json={"ontology_id": oid, "selected_topics": ["8"], "question_count": 1}).status_code == 422


def test_missing_definitions_handled_without_crash(upload):
    oid = upload(sample_graph(definitions=False))["id"]
    assert client.get(f"/api/meta?ontology_id={oid}").json()["definitions"] == 2
    sid = client.post("/api/sessions", json={"ontology_id": oid, "selected_topics": ["b"], "question_count": 1}).json()["session_id"]
    answer = client.post(f"/api/sessions/{sid}/ask", json={"question": "Что такое Бета?"}).json()
    assert "не заполнено определение" in answer["answer"]


def test_each_uploaded_graph_can_have_its_own_curriculum_order(upload):
    from graph import Ontology
    result = upload()["id"]
    folder = UPLOADS_DIR / result
    # Без файла приоритетов детей сортирует ID; дополнительная настройка меняет только новый граф.
    (folder / "curriculum_order.json").write_text('{"a": ["b"]}', encoding="utf-8")
    graph = Ontology(folder / "ontology.ont", relation_mapping={"contains": "has"})
    assert [step.node_id for step in graph.learning_path(["a"])] == ["a", "b", "c"]
    assert client.get("/api/meta").json()["nodes"] == 54


def test_demo_biology_ontology_uses_depth_first_after_mapping():
    from pathlib import Path
    from graph import Ontology
    graph = Ontology(Path(__file__).resolve().parents[1] / "examples" / "biology_demo.ont", relation_mapping={"contains": "has"})
    actual = [graph.nodes[step.node_id]["name"] for step in graph.learning_path(["01_cell"])]
    assert actual == ["Клетка", "Органоиды", "Ядро", "Митохондрия", "Рибосома", "Клеточная мембрана", "Цитоплазма"]


def test_first_question_selects_descendant_and_complete_branch_then_rechoose(upload):
    oid = upload()["id"]
    client.put(f"/api/ontologies/{oid}/relations", json={"mapping": {"contains": "has"}})
    sid = client.post('/api/sessions', json={'ontology_id': oid, 'selected_topics': ['a'], 'question_count': 6}).json()['session_id']
    initial = client.get(f'/api/sessions/{sid}').json()
    assert initial['mode'] == 'choose_start'
    assert initial['current'] is None
    assert initial['course_length'] == 0
    assert initial['allowed_count'] == 3
    assert not initial['can_finish']

    # Хотя корень A выбран в чекбоксах, УЧЕНИК начинает с его потомка B.
    first = client.post(f'/api/sessions/{sid}/ask', json={'question': 'Что такое Бета?'}).json()
    assert first['result'] == 'answered'
    assert first['answered_node']['id'] == 'b'
    assert first['state']['course_length'] == 2
    assert first['state']['mode'] == 'in_branch'
    assert first['state']['current']['id'] == 'c'
    assert not first['branch_completed']

    # Попытка перескочить на A при незавершённом обходе B -> C.
    jump = client.post(f'/api/sessions/{sid}/ask', json={'question': 'Что такое Альфа?'}).json()
    assert jump['result'] == 'out_of_order'
    assert jump['state']['questions_answered'] == 1

    last = client.post(f'/api/sessions/{sid}/ask', json={'question': 'Что такое Гамма?'}).json()
    assert last['result'] == 'answered'
    assert last['branch_completed']
    assert last['state']['mode'] == 'choose_start'
    assert last['state']['current'] is None
    assert last['state']['branches_completed'] == 1
    assert last['state']['questions_answered'] == 2

    # Дальше СВОБОДНО выбираем A — новая ветка A -> B -> C.
    other = client.post(f'/api/sessions/{sid}/ask', json={'question': 'Что такое Альфа?'}).json()
    assert other['result'] == 'answered'
    assert other['answered_node']['id'] == 'a'
    assert other['state']['mode'] == 'in_branch'
    assert other['state']['current']['id'] == 'b'
    assert other['state']['branch_number'] == 2
    assert other['state']['questions_answered'] == 3


def test_out_of_scope_and_ambiguous_start_not_counted(upload):
    oid = upload()['id']
    # 'a' пока не охватывает b и c, потому что contains не сопоставлен.
    sid = client.post('/api/sessions', json={'ontology_id': oid, 'selected_topics': ['a'], 'question_count': 2}).json()['session_id']
    unknown = client.post(f'/api/sessions/{sid}/ask', json={'question': 'Что такое?'}).json()
    assert unknown['result'] == 'unrecognized'
    assert unknown['state']['mode'] == 'choose_start'
    outside = client.post(f'/api/sessions/{sid}/ask', json={'question': 'Что такое Бета?'}).json()
    assert outside['result'] == 'out_of_scope'
    assert outside['state']['questions_answered'] == 0
    assert outside['state']['branch_number'] == 0
    allowed = client.post(f'/api/sessions/{sid}/ask', json={'question': 'Что такое Альфа?'}).json()
    assert allowed['result'] == 'answered'
    assert allowed['branch_completed'] is True
    assert allowed['state']['mode'] == 'choose_start'
    # После завершения одноузловой ветки можно выбрать её снова, счётчик общий.
    again = client.post(f'/api/sessions/{sid}/ask', json={'question': 'Расскажи про Альфа?'}).json()
    assert again['result'] == 'answered'
    assert again['state']['can_finish']
    assert again['state']['branches_completed'] == 2


def test_after_minimum_user_can_continue_any_branch(upload):
    oid = upload()['id']
    sid = client.post('/api/sessions', json={'ontology_id': oid, 'selected_topics': ['a', 'c'], 'question_count': 1}).json()['session_id']
    first = client.post(f'/api/sessions/{sid}/ask', json={'question': 'Что такое Гамма?'}).json()
    assert first['result'] == 'answered' and first['branch_completed']
    assert first['state']['can_finish'] and first['state']['active']
    follow = client.post(f'/api/sessions/{sid}/ask', json={'question': 'Что такое Альфа?'}).json()
    assert follow['result'] == 'answered'
    assert follow['state']['questions_answered'] == 2
    assert client.post(f'/api/sessions/{sid}/complete').status_code == 200


def test_duplicate_names_prefer_selected_node():
    from app import ontology
    assert ontology.nodes['65']['name'] == ontology.nodes['208']['name']
    assert ontology.matched_node('Что такое Функция активации?', preferred_ids={'208'}) == '208'
