"""Демонстрируем независимость от предметной области и источников в коде."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient
from app import app, ontology
from graph import Ontology
from tools.enrich_from_docx import read_document, enrich

client = TestClient(app)


def test_dynamic_parenthesized_name_and_inflection():
    for q in ('Что такое сигмоида?', 'Скажи про сигмоиды', 'Что такое логистическая функция?', 'Сигмоид'):
        assert ontology.matched_node(q) == '242', q


def test_nlp_search_no_subject_hardcoding():
    assert ontology.matched_node('Что такое нейросеть?') == '49'
    assert ontology.matched_node('Объясни нейросети') == '49'
    assert ontology.matched_node('Почему ветер дует?') is None
    response = client.get('/api/ontologies/default/search', params={'q': 'Что такое сигмоиды?'})
    assert response.status_code == 200
    assert response.json()['matched_id'] == '242'


def test_graph_all_and_three_levels():
    full = client.get('/api/ontologies/default/graph/242?depth=all').json()
    assert len(full['nodes']) == 54
    assert len(full['edges']) == 99
    assert not full['truncated']
    third = client.get('/api/ontologies/default/graph/242?depth=3').json()
    assert 9 < len(third['nodes']) <= len(full['nodes'])
    assert client.get('/api/ontologies/default/graph/242?depth=wrong').status_code == 422


def test_unknown_ontology_bracket_and_all_edges(tmp_path):
    raw = {'nodes': [
        {'id': 'a', 'name': 'Квантовый объект (квантон)', 'attributes': {'definition': 'Квантовый объект — это проверочное определение.'}},
        {'id': 'b', 'name': 'Эксперимент', 'attributes': {'definition': 'Эксперимент — это проверочный метод.'}},
        {'id': 'c', 'name': 'Другая тема', 'attributes': {'definition': 'Другая тема — это контрольный узел.'}},
    ], 'relations': [{'id': '1', 'name': 'use', 'source_node_id': 'a', 'destination_node_id': 'b'}]}
    source = tmp_path / 'unfamiliar.ont'; source.write_text(json.dumps(raw,ensure_ascii=False), encoding='utf-8')
    g = Ontology(source)
    assert g.matched_node('Что такое квантон?') == 'a'
    assert g.matched_node('Расскажи об экспериментах') == 'b'
    reply, provenance = g.response('a','Что такое квантон?')
    assert 'проверочное определение' in reply
    assert provenance[0]['field'] == 'attributes.definition'
    full = g.subgraph('a','all')
    assert len(full['nodes']) == 3 and len(full['edges']) == 1
    assert g.subgraph('a',1)['nodes'][0]['id'] == 'a'


def test_docx_enrichment_no_invented_definition(tmp_path):
    d = Document(); t=d.add_table(rows=1, cols=3)
    for cell, text in zip(t.rows[0].cells, ('Понятие','Категория','Определение')): cell.text=text
    row=t.add_row();row.cells[0].text='Тестовая теория';row.cells[1].text='Модель';row.cells[2].text='Это Теория из документа'
    doc = tmp_path / 'definitions.docx';d.save(doc)
    raw={'nodes':[{'id':'n','name':'Тестовая теория','attributes':{'def':'Неактуальное значение'}},
                  {'id':'m','name':'Отсутствует','attributes':{}}], 'relations':[]}
    report=enrich(raw,read_document(doc),{})
    assert raw['nodes'][0]['attributes'] == {'definition':'Тестовая теория — это теория из документа'}
    assert raw['nodes'][1]['attributes'] == {}
    assert [r['status'] for r in report] == ['matched','unmatched']


def test_training_category_and_depth_are_configurable():
    sid = client.post('/api/sessions', json={
        'selected_topics':['49'], 'question_count':1, 'search_depth':1,
        'question_category':'definition'}).json()['session_id']
    state=client.get(f'/api/sessions/{sid}').json()
    assert state['search_depth']==1 and state['question_category']=='definition'
    not_allowed=client.post(f'/api/sessions/{sid}/ask',json={'question':'Из чего состоит искусственная нейронная сеть?'}).json()
    assert not_allowed['result']=='unrecognized' and not_allowed['state']['questions_answered']==0
    allowed=client.post(f'/api/sessions/{sid}/ask',json={'question':'Что такое искусственная нейронная сеть?'}).json()
    assert allowed['result']=='answered'
