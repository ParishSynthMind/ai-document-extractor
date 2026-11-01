import pytest

from doc_extractor.schemas import SchemaError, available, load_schema, parse_schema


def _walk_objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for v in node.values():
            yield from _walk_objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_objects(v)


def test_builtin_schemas_load():
    assert available() == ["invoice", "permit_application", "staff_credential"]
    for name in available():
        s = load_schema(name)
        assert s.name == name and len(s.fields) >= 9


def test_json_schema_is_strict_everywhere():
    js = load_schema("invoice").json_schema()
    objects = list(_walk_objects(js))
    assert len(objects) == 1 + 11
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])


def test_value_types_follow_field_types():
    props = load_schema("staff_credential").json_schema()["properties"]
    assert props["background_check_cleared"]["properties"]["value"]["anyOf"][0] == {"type": "boolean"}
    assert props["licence_expiry"]["properties"]["value"]["anyOf"][0] == {"type": "string"}
    assert {"type": "null"} in props["full_name"]["properties"]["value"]["anyOf"]


@pytest.mark.parametrize("bad, msg", [
    ({"name": "x"}, "needs"),
    ({"name": "x", "fields": []}, "no fields"),
    ({"name": "x", "fields": [{"name": "a b"}]}, "identifier"),
    ({"name": "x", "fields": [{"name": "a", "type": "money"}]}, "unknown type"),
    ({"name": "x", "fields": [{"name": "a"}, {"name": "a"}]}, "duplicate"),
])
def test_bad_schemas_are_rejected(bad, msg):
    with pytest.raises(SchemaError, match=msg):
        parse_schema(bad)


def test_unknown_schema_name_lists_options():
    with pytest.raises(SchemaError, match="invoice"):
        load_schema("passport")


def test_schema_from_path(tmp_path):
    p = tmp_path / "receipt.json"
    p.write_text('{"name": "receipt", "fields": [{"name": "total", "type": "number", "required": true}]}')
    s = load_schema(str(p))
    assert s.field_names == ["total"] and s.fields[0].required
