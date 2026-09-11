"""Who we are matching for -- one identity per Telegram chat.

The watcher reads a single mailbox but serves several people, so identity
cannot be global. Each registered chat carries its own Neo ID, and a mail is
evaluated once per user. The owner's identity falls back to .env so a
single-user install needs no registration step.
"""
from . import config
from . import ids as ids_mod

FIELDS = ("neo_id", "reg_no", "name")
LABELS = {"neo_id": "Neo ID", "reg_no": "Registration no", "name": "Name"}
_DEFAULTS = {"neo_id": "NEO_ID", "reg_no": "REG_NO", "name": "FULL_NAME"}

# Legacy single-user keys, migrated into the owner's row on first use.
_LEGACY_PREFIX = "identity."


class InvalidValue(ValueError):
    pass


def ensure_owner(store):
    """Create the owner row, carrying over any pre-multi-user settings."""
    chat_id = config.TELEGRAM_CHAT_ID
    if not chat_id:
        return None
    if store.get_user(chat_id) is None:
        store.add_user(chat_id, is_owner=True)
        for field in FIELDS:
            legacy = store.get(_LEGACY_PREFIX + field)
            if legacy:
                store.set_user_field(chat_id, field, legacy)
        legacy_extra = store.get(_LEGACY_PREFIX + "extra_ids")
        if legacy_extra:
            store.set_user_field(chat_id, "extra_ids", legacy_extra)
    return chat_id


def current(store, chat_id):
    """Identity for one chat. Owner falls back to .env; others do not."""
    row = store.get_user(chat_id) or {}
    is_owner = bool(row.get("is_owner"))
    out = {}
    for field in FIELDS:
        value = row.get(field) or ""
        if not value and is_owner:
            value = getattr(config, _DEFAULTS[field], "")
        out[field] = value
    out["extra_ids"] = _split(row.get("extra_ids"))
    return out


def is_usable(me):
    """Enough to match on? Name alone is too weak to alert from."""
    return bool(me.get("neo_id") or me.get("reg_no") or me.get("extra_ids"))


def _split(raw):
    return [t for t in (x.strip().upper() for x in (raw or "").split(",")) if t]


def validate(field, value):
    value = (value or "").strip()
    if not value:
        raise InvalidValue("value is empty")
    if field == "neo_id":
        value = value.upper()
        if not ids_mod.NEO_ID_RE.fullmatch(value):
            raise InvalidValue(
                "a Neo ID is 8 characters alternating letter/digit, e.g. B5R7O9J8"
            )
    elif field == "reg_no":
        value = value.upper()
        if not ids_mod.REG_NO_RE.fullmatch(value):
            raise InvalidValue("a registration number looks like 23BCE7625")
    elif field == "name":
        if len(value) < 3:
            raise InvalidValue("name is too short")
    return value


def set_field(store, chat_id, field, value):
    if field not in FIELDS:
        raise InvalidValue("unknown field: {}".format(field))
    value = validate(field, value)
    store.set_user_field(chat_id, field, value)
    return value


def add_extra_id(store, chat_id, value):
    value = (value or "").strip().upper()
    if not ids_mod.EXTRA_ID_RE.fullmatch(value):
        raise InvalidValue(
            "a reference id is at least 5 characters, letters and digits only "
            "-- e.g. CT20264998331 (TCS) or 8254250 (Superset)"
        )
    row = store.get_user(chat_id) or {}
    ids = _split(row.get("extra_ids"))
    if value not in ids:
        ids.append(value)
    store.set_user_field(chat_id, "extra_ids", ",".join(ids))
    return value


def remove_extra_id(store, chat_id, value):
    value = (value or "").strip().upper()
    row = store.get_user(chat_id) or {}
    kept = [t for t in _split(row.get("extra_ids")) if t != value]
    store.set_user_field(chat_id, "extra_ids", ",".join(kept))
    return value
