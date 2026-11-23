"""Регрессии по замечанию преподавателя: длинные термины, ветви, тройки."""
import json

from fastapi.testclient import TestClient
from app import app, ontology
from graph import Ontology

client = TestClient(app)


def test_long_concept_outweighs_root_and_spelling_errors():
    assert ontology.matched_node('алгоритм обратного распространения') == '109'
    assert ontology.matched_node('Что такое алгоритм обратного распространения ошибки?') == '109'
    assert ontology.matched_node('Что такое нейрон?') == '64'
    assert ontology.matched_node('как работает инс') == '49'
    assert ontology.matched_node('Что такое сигмоиды') == '242'


def test_answer_is_ordered_and_graph_grounded():
    text, sources = ontology.response('49', 'Как работает ИНС?')
    assert text.index('Вопрос:') < text.index('Определение:') < text.index('Ответ:')
    assert 'Синапс' in text and 'Математическая модель' in text
    assert 'Слой нейронов' in text and 'Нейрон' in text
    assert 'Функция активации' in text
    assert any('relation' in s for s in sources)


def test_original_graph_triples_follow_arrow_direction():
    triples = client.get('/api/ontologies/default/triples',
                         params={'node_id': '49', 'depth': 1}).json()['items']
    assert any(t['subject_id'] == '49' and t['predicate'] == 'has'
               and t['object_id'] == '53' for t in triples)
    assert any(t['subject_id'] == '49' and t['predicate'] == 'is_instance'
               and t['object_id'] == '60' for t in triples)


def test_new_algorithm_branch_and_context_without_hardcoding(tmp_path):
    """Промежуточная вершина добавлена после разработки программы."""
    raw = {
        'nodes': [
            {'id': 'root', 'name': 'Алгоритм', 'attributes': {'definition': 'Общее предписание.'}},
            {'id': 'middle', 'name': 'Алгоритм обучения', 'attributes': {'definition': 'Процедура изменения параметров.'}},
            {'id': 'leaf', 'name': 'Алгоритм обратного распространения ошибки (Backdrop)',
             'attributes': {'definition': 'Получение градиентов для изменения параметров.'}},
            {'id': 'task', 'name': 'Практическая задача', 'attributes': {'definition': 'Проверочная задача.'}},
        ],
        'relations': [
            {'id': 'r1', 'name': 'is_a', 'source_node_id': 'middle', 'destination_node_id': 'root'},
            {'id': 'r2', 'name': 'is_a', 'source_node_id': 'leaf', 'destination_node_id': 'middle'},
            {'id': 'r3', 'name': 'used_for', 'source_node_id': 'leaf', 'destination_node_id': 'task'},
        ],
    }
    path = tmp_path / 'new_domain.ont'
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
    graph = Ontology(path)
    assert graph.matched_node('Что такое алгоритм обратного распространения?') == 'leaf'
    assert graph.matched_node('Что такое алгоритм обучения?') == 'middle'
    assert graph.matched_node('алгоритм', preferred_ids={'root', 'middle', 'leaf'}) == 'root'
    assert [step.node_id for step in graph.learning_path(['root'])] == ['root', 'middle', 'leaf', 'task']
    assert graph.graph_distance('middle', 'leaf') == 1
    answer, src = graph.response('leaf', 'Для чего используется алгоритм обратного распространения?')
    assert 'Практическая задача' in answer
    assert 'Получение градиентов' in answer
    assert 'Общее предписание' not in answer
    triples = graph.semantic_triples('middle', depth=1)
    assert any(f['subject_id'] == 'leaf' and f['object_id'] == 'middle' for f in triples)


def test_real_api_new_ontology_next_neighbor_can_be_studied():
    payload = {
        'nodes': [
            {'id': '1', 'name': 'Механизм', 'attributes': {'definition': 'Общий механизм.'}},
            {'id': '2', 'name': 'Механизм обучения', 'attributes': {'definition': 'Частный механизм.'}},
            {'id': '3', 'name': 'Механизм обратного хода (реверс)',
             'attributes': {'definition': 'Алгоритм обратного хода.'}},
        ],
        'relations': [
            {'id': 'a', 'name': 'is_a', 'source_node_id': '2', 'destination_node_id': '1'},
            {'id': 'b', 'name': 'is_a', 'source_node_id': '3', 'destination_node_id': '2'},
        ],
    }
    upload = client.post('/api/ontologies', files={
        'file': ('synthetic_v7.ont', json.dumps(payload, ensure_ascii=False).encode('utf-8'), 'application/json')
    })
    assert upload.status_code == 201
    oid = upload.json()['id']
    sid = client.post('/api/sessions', json={
        'ontology_id': oid, 'selected_topics': ['1'], 'question_count': 3,
    }).json()['session_id']
    first = client.post(f'/api/sessions/{sid}/ask', json={'question': 'Что такое механизм?'}).json()
    assert first['result'] == 'answered'
    assert first['state']['current']['id'] == '2'
    # После общего понятия следующий узел по ветке можно назвать напрямую.
    jump = client.post(f'/api/sessions/{sid}/ask', json={
        'question': 'Что такое механизм обратного хода?'
    }).json()
    assert jump['result'] == 'answered'
    assert jump['answered_node']['id'] == '3'
    assert jump['state']['questions_answered'] == 2
    assert jump['semantic_triples']
