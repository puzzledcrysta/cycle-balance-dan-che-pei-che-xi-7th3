import json
from app.errors import DataError
from app.store import connection


def lookup(db, handover_id):
    row = db.execute('SELECT * FROM handover_runs WHERE id=?', (handover_id,)).fetchone()
    if row is None:
        raise DataError('HANDOVER_UNKNOWN', '没有此交接批次', 404)
    return row


def fetch(handover_id):
    with connection() as db:
        return lookup(db, handover_id)


def persist(handover_id, opening, departure, event, result, created):
    with connection() as db:
        db.execute('INSERT OR IGNORE INTO handover_runs VALUES (?,?,?,?,?,?)',
                   (handover_id, opening, departure, event,
                    json.dumps(result, ensure_ascii=False), created))
        return lookup(db, handover_id)


def history():
    with connection() as db:
        return [dict(row) for row in db.execute(
            'SELECT id,created FROM handover_runs ORDER BY created DESC,id DESC LIMIT 50')]
