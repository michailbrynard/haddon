"""Expose Haddon's write commands to an LLM agent as tools, generated from the service's OpenAPI document.

This is an example, not part of the package: copy it into your agent harness and adapt it. It has no
dependencies beyond the standard library.

Every write route has an ``operationId`` equal to its command name (``post_entry``, ``book_line``, ...), and its
request body schema is self-contained, so one tool per command falls out of ``/openapi.json`` directly. The tool
definitions use the ``name`` / ``description`` / ``input_schema`` shape of the Anthropic Messages API; other
providers take the same three things under different keys.

    tools, routes = load_tools("http://127.0.0.1:8080", allow={"post_entry", "book_line", "ignore_line"})
    # ... pass `tools` to the model; for each tool call it makes:
    result = call(routes, name, args, entity="acme", token=TOKEN, call_id=tool_call_id)

Reads are left to you: most agents do well with one generic GET tool over the paths ``/capabilities`` lists.
See docs/agent-guide.md.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

ENTITY_PREFIX = "/v1/entities/{entity_id}"


def tools_from_openapi(spec: dict[str, Any], allow: set[str] | None = None
                       ) -> tuple[list[dict[str, Any]], dict[str, tuple[str, list[str]]]]:
    """One tool per entity-scoped write command, and the route each tool calls.

    ``allow`` restricts the set: give an agent only the commands you want it to have (for example, leave out
    ``close_period`` and ``reopen_period`` if closing is a human decision in your process).
    """
    tools: list[dict[str, Any]] = []
    routes: dict[str, tuple[str, list[str]]] = {}
    for path, operations in spec["paths"].items():
        op = operations.get("post")
        if op is None or not path.startswith(ENTITY_PREFIX):
            continue
        name = op["operationId"]
        if allow is not None and name not in allow:
            continue
        params = [p["name"] for p in op.get("parameters", [])
                  if p["in"] == "path" and p["name"] != "entity_id"]
        body = dict(op["requestBody"]["content"]["application/json"]["schema"])
        defs = body.pop("$defs", {})  # "$ref": "#/$defs/..." resolves from the root of the tool's schema
        schema: dict[str, Any] = {"type": "object",
                                  "properties": {**{p: {"type": "string"} for p in params}, "body": body},
                                  "required": [*params, "body"]}
        if defs:
            schema["$defs"] = defs
        tools.append({"name": name,
                      "description": f"{op.get('summary', '')}\n\n{op.get('description', '')}".strip(),
                      "input_schema": schema})
        routes[name] = (path, params)
    return tools, routes


def load_tools(base: str, allow: set[str] | None = None):
    """``tools_from_openapi`` over a running service's ``/openapi.json``."""
    with urllib.request.urlopen(f"{base}/openapi.json") as response:
        return tools_from_openapi(json.load(response), allow)


def call(routes: dict[str, tuple[str, list[str]]], name: str, args: dict[str, Any], *, entity: str, token: str,
         call_id: str, base: str = "http://127.0.0.1:8080") -> dict[str, Any]:
    """Run one tool call and return the JSON reply, success or refusal.

    The tool call's own id is the ``Idempotency-Key``: if the HTTP request is retried after a timeout, the
    service replays the first result instead of writing twice. A refusal is returned rather than raised, because
    the error envelope (``code``, ``message``, ``details``) is what the model needs to read to fix its call.
    """
    path, params = routes[name]
    url = base + path.replace("{entity_id}", entity)
    for p in params:
        url = url.replace("{" + p + "}", urllib.request.quote(args[p], safe=""))
    request = urllib.request.Request(
        url, method="POST", data=json.dumps(args["body"]).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Idempotency-Key": call_id})
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as refused:
        return json.load(refused)
