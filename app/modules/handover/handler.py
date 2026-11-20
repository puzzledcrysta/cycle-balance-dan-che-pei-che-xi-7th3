from base64 import b64encode
from fastapi import APIRouter
from app.inputs import Handover
from .ledger import run, get
from .repository import fetch, history

router = APIRouter()


@router.post('/api/handover')
def create(payload: Handover):
    return run(payload.opening_bytes(), payload.departure_bytes(),
               payload.event_bytes(), payload.cutoff or '')


@router.get('/api/handover')
def list_handover():
    return history()


@router.get('/api/handover/{handover_id}')
def detail(handover_id: str):
    return get(handover_id)


@router.get('/api/handover/{handover_id}/source')
def source_inputs(handover_id: str):
    record = fetch(handover_id)
    return {'opening_base64': b64encode(record['opening']).decode(),
            'departure_base64': b64encode(record['departure']).decode(),
            'event_base64': b64encode(record['event']).decode()}
