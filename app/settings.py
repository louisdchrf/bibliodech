import json
from sqlalchemy.orm import Session
from app.models import Setting

DEFAULTS = {
    "timezone": "Europe/Paris",
    "lookup_sources": [
        {"id": "sudoc",       "label": "SUDOC",        "enabled": True,  "timeout": 5},
        {"id": "bnf",         "label": "BNF",          "enabled": True,  "timeout": 5},
        {"id": "decitre",     "label": "Decitre",      "enabled": True,  "timeout": 10},
        {"id": "isbndb",      "label": "ISBNdb",       "enabled": False, "timeout": 5},
        {"id": "openlibrary",        "label": "Open Library",        "enabled": False, "timeout": 5},
        {"id": "openlibrary_search", "label": "Open Library (search)", "enabled": True,  "timeout": 8},
        {"id": "googlebooks",        "label": "Google Books",          "enabled": True,  "timeout": 5},
    ],
    "isbndb_api_key": "",
    "googlebooks_api_key": "",
    "smtp": {
        "host": "", "port": 587, "user": "", "password": "",
        "from_": "", "tls": True,
    },
    "mail_overdue":      False,
    "mail_new_account":  False,
    "mail_reset_password": False,
    "site_url": "",
}


def get(db: Session, key: str):
    row = db.query(Setting).filter(Setting.key == key).first()
    if row:
        return json.loads(row.value)
    return DEFAULTS.get(key)


def set_(db: Session, key: str, value) -> None:
    row = db.query(Setting).filter(Setting.key == key).first()
    if row:
        row.value = json.dumps(value)
    else:
        db.add(Setting(key=key, value=json.dumps(value)))
    db.commit()


def get_all(db: Session) -> dict:
    rows = db.query(Setting).all()
    stored = {r.key: json.loads(r.value) for r in rows}
    return {**DEFAULTS, **stored}
