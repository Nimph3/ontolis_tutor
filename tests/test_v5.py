"""Повторные проверки правок интерфейса, определений и отчета о качестве."""
import json
import re
from pathlib import Path
from fastapi.testclient import TestClient
from app import app, ontology

client = TestClient(app)


def test_function_activation_has_identical_requested_definitions():
    target = 'Функция активации — это функция, преобразующая входной сигнал нейрона и определяющая его выходное значение'
    for ident in ('65', '208'):
        assert ontology.nodes[ident]['attributes']['def'] == target
    raw = json.loads((Path(__file__).resolve().parents[1] / 'data' / 'ontology.ont').read_text('utf-8'))
    stored = [n for n in raw['nodes'] if str(n['id']) in {'65', '208'}]
    assert len(stored) == 2
    assert all(n['attributes']['def'] == target for n in stored)


def test_definitions_have_lowercase_after_dash_or_eto():
    for node in ontology.nodes.values():
        definition = node['attributes']['def']
        assert not re.search(r'(?:\bэто\s+|\s[—–-]\s+)[А-ЯЁ][а-яё]+', definition)


def test_no_unwanted_purpose_message_for_definition():
    answer, _ = ontology.response('64', 'Что такое нейрон?')
    assert 'Назначение: в этой онтологии' not in answer
    assert 'в этой онтологии необходимые сведения не указаны' not in answer
    purpose, _ = ontology.response('64', 'Для чего нужен нейрон?')
    assert 'В онтологии не указано, какие задачи' not in purpose


def test_unnamed_relations_are_explained_with_endpoints():
    data = client.get('/api/ontologies/default/quality').json()
    assert set(data['unnamed_relation_ids']) == {'231', '236'}
    assert set(data['unmapped_relation_ids']) == {'231', '236'}
    assert {r['id'] for r in data['unnamed_relation_details']} == {'231', '236'}
    assert all(r['source'] == 'Скорость обучения' for r in data['unnamed_relation_details'])
    assert any('Классификация чисел' in r['destination'] for r in data['unnamed_relation_details'])


def test_updated_ui_and_graph_implementation():
    home = client.get('/').text
    js = client.get('/static/app.js').text
    css = client.get('/static/styles.css').text
    assert 'OntoTutor — интерактивная обучающая система на основе графа знаний' in home
    assert 'class="graph-layout"' in home
    assert "Начнём изучение. Задайте первый вопрос" in js
    assert 'Источники и путь по графу' not in js
    assert 'Начнём изучать' not in js
    assert "svgElement('ellipse'" in js
    assert 'graph-edge-label' in js
    assert '.graph-layout' in css
