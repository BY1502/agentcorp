from uuid import uuid4
from fastapi.testclient import TestClient
from app.main import app

client=TestClient(app)

def test_health_endpoint():
    r=client.get('/health'); assert r.status_code==200 and r.json()=={'status':'ok'}

def test_create_and_get_mission():
    created=client.post('/missions',json={'title':'api demo'}); assert created.status_code==200
    data=created.json(); fetched=client.get('/missions/'+data['id']); assert fetched.status_code==200; assert fetched.json()['title']=='api demo'

def test_run_mission_endpoint():
    mission=client.post('/missions',json={}).json(); r=client.post('/missions/'+mission['id']+'/runs'); data=r.json()
    assert r.status_code==200 and data['status']=='PASSED' and data['mission_id']==mission['id']; assert data['changed_files'] and data['tool_call_count']>0 and data['event_count']>0

def test_get_run():
    mission=client.post('/missions',json={}).json(); run=client.post('/missions/'+mission['id']+'/runs').json(); r=client.get('/runs/'+run['run_id'])
    assert r.status_code==200 and r.json()==run

def test_get_run_events():
    mission=client.post('/missions',json={}).json(); run=client.post('/missions/'+mission['id']+'/runs').json(); r=client.get('/runs/'+run['run_id']+'/events'); events=r.json()
    assert r.status_code==200 and events and [x['sequence'] for x in events]==sorted(x['sequence'] for x in events); assert events[0]['event_type']=='mission_started' and events[-1]['event_type']=='mission_finished'; assert len(events)==run['event_count']

def test_unknown_mission_returns_404():
    missing=str(uuid4()); assert client.get('/missions/'+missing).status_code==404; assert client.post('/missions/'+missing+'/runs').status_code==404

def test_unknown_run_returns_404():
    missing=str(uuid4()); assert client.get('/runs/'+missing).status_code==404; assert client.get('/runs/'+missing+'/events').status_code==404
