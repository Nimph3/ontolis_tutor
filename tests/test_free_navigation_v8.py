"""V8: ответ без усечения и свободная навигация по любой ONTOLIS-онтологии."""
import json
import shutil
from fastapi.testclient import TestClient
from app import app, ontology, ontologies, sessions, UPLOADS_DIR

client = TestClient(app)


def ask(sid, question):
    response = client.post(f'/api/sessions/{sid}/ask', json={'question': question})
    assert response.status_code == 200, response.text
    return response.json()


def test_neural_network_siblings_parent_and_backtracking():
    sid = client.post('/api/sessions', json={
        'selected_topics': ['49'], 'question_count': 5
    }).json()['session_id']
    start = ask(sid, 'как работает инс')
    assert start['answered_node']['id'] == '49'
    assert start['state']['current']['id'] == '49'
    assert start['state']['neighbor_count'] >= 4
    assert {'53', '63', '60', '65'} <= {n['id'] for n in start['state']['neighbors']}
    # Проверяем сохранение ПОЛНОГО определения «Слой нейронов».
    assert 'передаёт результат следующему нейрону' in start['answer']
    assert 'обрабатывает…' not in start['answer']

    destinations = [
        ('Что такое математическая модель?', '60'),
        ('Что такое слой нейронов?', '63'),
        ('Что такое нейрон?', '64'),
        ('Что такое синапс?', '53'),
        ('Что такое искусственная нейронная сеть?', '49'),
    ]
    for question, expected in destinations:
        response = ask(sid, question)
        assert response['result'] == 'answered'
        assert response['answered_node']['id'] == expected
        assert response['state']['current']['id'] == expected
    assert response['state']['questions_answered'] == 6
    assert response['state']['can_finish']


def test_multi_domain_inbound_outbound_siblings_and_complete_definition():
    long_definition = ('Орган управления в системе, ' + 'передающий сигналы между частями. ' * 18
                       + 'Финальное предложение не должно пропасть.')
    payload = {
        'nodes': [
            {'id': 'control', 'name': 'Система управления',
             'attributes': {'definition': 'Система, согласующая работу устройств.'}},
            {'id': 'sensor', 'name': 'Датчик',
             'attributes': {'definition': 'Устройство измерения параметров.'}},
            {'id': 'hub', 'name': 'Контроллер',
             'attributes': {'definition': long_definition}},
            {'id': 'engine', 'name': 'Исполнительный механизм',
             'attributes': {'definition': 'Устройство, исполняющее команды.'}},
            {'id': 'external', 'name': 'Изолированное понятие',
             'attributes': {'definition': 'Не связано с системой.'}},
        ],
        'relations': [
            {'id': 'r1', 'name': 'contains', 'source_node_id': 'control', 'destination_node_id': 'sensor'},
            {'id': 'r2', 'name': 'has', 'source_node_id': 'control', 'destination_node_id': 'hub'},
            {'id': 'r3', 'name': 'use', 'source_node_id': 'hub', 'destination_node_id': 'engine'},
        ]
    }
    created = client.post('/api/ontologies', files={
        'file': ('arbitrary_ontology.ont', json.dumps(payload, ensure_ascii=False).encode(),
                 'application/octet-stream')
    })
    assert created.status_code == 201, created.text
    oid = created.json()['id']
    try:
        sid = client.post('/api/sessions', json={
            'ontology_id': oid, 'selected_topics': ['control'],
            'question_count': 4,
        }).json()['session_id']
        first = ask(sid, 'Что такое система управления?')
        assert {n['id'] for n in first['state']['neighbors']} == {'sensor', 'hub'}
        # «contains» — незнакомый тип. Навигацию это не блокирует.
        sensor = ask(sid, 'Что такое датчик?')
        assert sensor['transition'] == 'neighbor'
        back = ask(sid, 'Что такое система управления?')
        assert back['transition'] == 'neighbor'
        hub = ask(sid, 'Что такое контроллер?')
        assert hub['transition'] == 'neighbor'
        assert 'Финальное предложение не должно пропасть.' in hub['answer']
        engine = ask(sid, 'Что такое исполнительный механизм?')
        assert engine['transition'] == 'neighbor'
        assert engine['state']['current']['id'] == 'engine'
        outside = ask(sid, 'Что такое изолированное понятие?')
        assert outside['result'] == 'out_of_scope'
        assert outside['state']['questions_answered'] == 5
    finally:
        for sid in list(sessions):
            if sessions[sid].ontology_id == oid:
                del sessions[sid]
        ontologies.pop(oid, None)
        shutil.rmtree(UPLOADS_DIR / oid, ignore_errors=True)


def test_depth_limit_is_graph_distance_not_dfs_order():
    sid = client.post('/api/sessions', json={
        'selected_topics': ['49'], 'question_count': 2,
        'search_depth': 1,
    }).json()['session_id']
    response = ask(sid, 'Как работает ИНС?')
    assert response['state']['allowed_count'] == 5  # центр и 4 соседа
    assert {n['id'] for n in response['state']['neighbors']} == {'60', '53', '63', '65'}
    outside = ask(sid, 'Что такое нейрон?')
    assert outside['result'] == 'out_of_scope'  # 2 шага от ИНС
    assert outside['state']['questions_answered'] == 1
