import json
from datetime import datetime, timezone
from hashlib import sha256
from app.settings import HANDOVER_RULE_VERSION
from .service import produce
from .repository import fetch, persist

HANDOVER_KEY = 'handover'


def fingerprint(opening, departure, event, cutoff_text):
    digest = sha256()
    for blob in (opening, departure, event):
        digest.update(sha256(blob).hexdigest().encode())
    digest.update((cutoff_text or '').encode())
    digest.update(HANDOVER_RULE_VERSION.encode())
    return digest.hexdigest()


def run(opening, departure, event, cutoff_text=''):
    handover_id = fingerprint(opening, departure, event, cutoff_text)
    result = produce(opening, departure, event, cutoff_text)
    created = datetime.now(timezone.utc).isoformat()
    result['manifest'] = {
        'handover_id': handover_id,
        'opening_sha256': sha256(opening).hexdigest(),
        'departure_sha256': sha256(departure).hexdigest(),
        'event_sha256': sha256(event).hexdigest(),
        'cutoff_input': cutoff_text or None,
        'version': HANDOVER_RULE_VERSION,
        'created': created,
    }
    # 同输入同截止时刻：INSERT OR IGNORE 命中既有快照，重跑不产生第二次结转
    stored = persist(handover_id, opening, departure, event, result, created)
    return json.loads(stored['result'])


def get(handover_id):
    return json.loads(fetch(handover_id)['result'])
