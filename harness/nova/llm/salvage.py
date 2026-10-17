"""Make a model's answer fit its tool schema with the smallest safe changes, instead of losing the whole document.

An answer that breaks the schema in one corner (one rebate tier with the wrong field names, a list left out because
the document has nothing for it) used to fail the whole read: an 11-page contract lost over one tier. `salvage` keeps
everything that is valid:

- a required list that is missing becomes an empty list (the document had nothing for it);
- a value that is wrong and not required is dropped;
- an item of a list that can't be made valid is dropped, with the rest of the list kept;
- anything else (a required single value missing or wrong at the top) can't be saved: None.

Every change is written down; the reader records them as warnings and marks the read partial when something was
dropped, so nothing disappears silently.
"""
from __future__ import annotations

import copy
from typing import Any

import jsonschema

EMPTY_LIST = "empty list for"     # the prefix of notes that only filled in a list the document has nothing for


def _types(schema: dict) -> set[str]:
    t = schema.get("type") if isinstance(schema, dict) else None
    return set(t) if isinstance(t, list) else {t} if t else set()


def _schema_at(root: dict, path: list) -> dict:
    """The schema of the value at `path` (object keys and list indexes), or {} if it can't be followed."""
    s = root
    for step in path:
        if not isinstance(s, dict):
            return {}
        if isinstance(step, int):
            s = s.get("items") or {}
        else:
            s = (s.get("properties") or {}).get(step) or {}
    return s


def _get(data: Any, path: list) -> Any:
    for step in path:
        data = data[step]
    return data


def _drop(data: Any, path: list) -> None:
    parent = _get(data, path[:-1])
    if isinstance(parent, list):
        parent.pop(path[-1])
    else:
        parent.pop(path[-1], None)


def _required_by_parent(root: dict, path: list) -> bool:
    """Is the value at `path` required by the object that holds it? (List items are never 'required'.)"""
    if not path or isinstance(path[-1], int):
        return False
    return path[-1] in (_schema_at(root, path[:-1]).get("required") or [])


def _nearest_item(path: list) -> list | None:
    """The path of the innermost list item containing `path`, or None."""
    for i in range(len(path) - 1, -1, -1):
        if isinstance(path[i], int):
            return path[:i + 1]
    return None


def salvage(data: Any, schema: dict, max_changes: int = 500) -> tuple[dict | None, list[str]]:
    if not isinstance(data, dict):
        return None, ["the answer is not an object"]
    data = copy.deepcopy(data)
    validator = jsonschema.Draft202012Validator(schema)
    notes: list[str] = []
    for _ in range(max_changes):
        errors = sorted(validator.iter_errors(data), key=lambda e: -len(e.absolute_path))
        if not errors:
            return data, notes
        e = errors[0]
        path = list(e.absolute_path)
        where = "/".join(map(str, path)) or "(top level)"
        if e.validator == "required":
            missing = next((name for name in e.validator_value if isinstance(e.instance, dict) and name not in e.instance),
                           None)
            if missing is not None and "array" in _types(_schema_at(schema, path + [missing])):
                _get(data, path)[missing] = []
                notes.append(f"{EMPTY_LIST} {'/'.join(map(str, path + [missing]))}")
                continue
            # a required single value is missing: the object that needs it can only go if it is a list item
            item = _nearest_item(path) if path else None
            if item is None:
                return None, notes + [f"{where}: {e.message}"]
            _drop(data, item)
            notes.append(f"dropped {'/'.join(map(str, item))}: {e.message}")
            continue
        # a wrong value: drop it if nothing requires it, else drop the list item it lives in
        if path and not _required_by_parent(schema, path):
            _drop(data, path)
            notes.append(f"dropped {where}: {e.message}")
            continue
        item = _nearest_item(path)
        if item is None:
            return None, notes + [f"{where}: {e.message}"]
        _drop(data, item)
        notes.append(f"dropped {'/'.join(map(str, item))}: {e.message}")
    return None, notes + ["too many problems to repair"]


def only_filled_lists(notes: list[str]) -> bool:
    return bool(notes) and all(n.startswith(EMPTY_LIST) for n in notes)
