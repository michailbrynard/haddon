"""``examples/haddon_tools.py`` turns the OpenAPI document into one tool per write command.

The agent guide points people at it, so it is held to the published surface: every entity-scoped command
becomes exactly one tool under its own name, and every ``$ref`` in a generated schema resolves inside that
schema (a tool definition is sent to a model on its own, with nothing around it to resolve a reference against).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from haddon.api import surface
from haddon.api.app import create_app

# Building the OpenAPI document takes seconds, so the module shares one and sits in the slow tier.
pytestmark = pytest.mark.slow

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "haddon_tools.py"


@pytest.fixture(scope="module")
def app():
    return create_app()


@pytest.fixture(scope="module")
def spec(app):
    return app.openapi()


def _example():
    spec = importlib.util.spec_from_file_location("haddon_tools_example", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _refs(node):
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            yield node["$ref"]
        for value in node.values():
            yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


def test_every_published_command_becomes_one_tool_under_its_own_name(app, spec):
    tools, routes = _example().tools_from_openapi(spec)
    _, writes = surface.published(app)
    assert sorted(t["name"] for t in tools) == sorted(w.command for w in writes)
    assert set(routes) == {t["name"] for t in tools}
    for tool in tools:
        assert tool["description"], tool["name"]
        assert tool["input_schema"]["required"][-1] == "body"


def test_every_reference_in_a_tool_schema_resolves_within_it(spec):
    tools, _ = _example().tools_from_openapi(spec)
    for tool in tools:
        schema = tool["input_schema"]
        for ref in _refs(schema):
            assert ref.startswith("#/$defs/"), (tool["name"], ref)
            assert ref.removeprefix("#/$defs/") in schema.get("$defs", {}), (tool["name"], ref)


def test_allow_restricts_the_tools_an_agent_is_given(spec):
    tools, routes = _example().tools_from_openapi(spec, allow={"post_entry", "book_line"})
    assert {t["name"] for t in tools} == set(routes) == {"post_entry", "book_line"}
