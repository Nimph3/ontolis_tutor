from fastapi.testclient import TestClient

from app import app, ontology

client = TestClient(app)


def create(roots=None, count=2):
    result=client.post('/api/sessions',json={'selected_topics':roots or ['8'], 'question_count':count})
    assert result.status_code==200,result.text
    return result.json()


def test_source_data():
    m=client.get('/api/meta').json()
    assert m['nodes']==54 and m['relations']==99
    assert len(m['topics'])==54
    assert ontology.nodes['64']['attributes']['def'].startswith('Нейрон - это')
    assert all('def' in node['attributes'] for node in ontology.nodes.values())


def test_order_is_depth_first_and_reverse_part_of():
    path=ontology.learning_path(['8'])
    ids=[s.node_id for s in path]
    assert ids[0:2]==['8','111']
    assert '112' in ids and '113' in ids
    assert ids.index('111')<ids.index('112')<ids.index('113')
    assert path[1].via_relation_id=='114'  # a_part_of: 111 -> 8, обход наоборот
    assert len(ids)==len(set(ids))


def test_def_and_relation_trace():
    created=create(['49'],2)
    sid=created['session_id']
    response=client.post(f'/api/sessions/{sid}/ask',json={'question':'Из чего состоит Искусственная нейронная сеть?'})
    assert response.status_code==200
    result=response.json()
    assert result['result']=='answered'
    assert 'Математическая модель' in result['answer']
    assert 'Слой нейронов' in result['answer']
    assert result['sources'][0]=={'node_id':'49','field':'attributes.def'}
    assert any(x.get('relation',{}).get('id')=='66' for x in result['sources'])
    assert result['state']['questions_answered']==1


def test_finish_only_after_quota():
    sid=create(['64'],2)['session_id']
    before=client.post(f'/api/sessions/{sid}/complete')
    assert before.status_code==409
    for i in range(2):
        result=client.post(f'/api/sessions/{sid}/ask',json={'question':'Что такое нейрон?'})
        assert result.status_code==200
        assert result.json()['result']=='answered'
    done=client.post(f'/api/sessions/{sid}/complete')
    assert done.status_code==200
    assert done.json()['active'] is False
    assert done.json()['reason']=='completed'
    assert client.post(f'/api/sessions/{sid}/ask',json={'question':'Что такое нейрон?'}).status_code==409


def test_out_of_order_does_not_count():
    sid=create(['8'],2)['session_id']
    first=client.post(f'/api/sessions/{sid}/ask',json={'question':'Что такое Лабораторный практикум?'})
    assert first.json()['result']=='answered'
    # После выбора 8 (Лабораторный практикум) текущий шаг 111, нельзя перескочить к 113.
    now=first.json()['state']['current']['id']
    assert now=='111'
    response=client.post(f'/api/sessions/{sid}/ask',json={'question':f'Что такое {ontology.nodes["113"]["name"]}?'})
    assert response.status_code==200
    body=response.json()
    assert body['result']=='out_of_order'
    assert body['state']['questions_answered']==1
    assert body['state']['current']['id']==now


def test_early_finish_unlocks():
    sid=create(['8'],30)['session_id']
    result=client.post(f'/api/sessions/{sid}/force')
    assert result.status_code==200
    assert result.json()['reason']=='forced'
    assert client.post(f'/api/sessions/{sid}/complete').status_code==409


def test_invalid_user_input():
    assert client.post('/api/sessions',json={'selected_topics':[], 'question_count':1}).status_code==422
    assert client.post('/api/sessions',json={'selected_topics':['1234567890'],'question_count':1}).status_code==422
    assert client.post('/api/sessions',json={'selected_topics':['8'],'question_count':0}).status_code==422
    sid=create(['8'],2)['session_id']
    assert client.post(f'/api/sessions/{sid}/ask',json={'question':'Hello'}).status_code==422


def test_home_and_frontend():
    assert client.get('/').status_code==200
    assert 'Онтологический тьютор' in client.get('/').text
    assert client.get('/static/app.js').status_code==200


def test_unknown_question_not_counted():
    sid=create(['8'],2)['session_id']
    resp=client.post(f'/api/sessions/{sid}/ask',json={'question':'Почему ветер дует?'})
    assert resp.status_code==200
    assert resp.json()['result']=='unrecognized'
    assert resp.json()['state']['questions_answered']==0


def test_short_lab_name():
    assert ontology.matched_node('Какие входные данные у ЛР5?', '112')=='112'
