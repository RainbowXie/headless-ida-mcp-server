# -*- coding: utf-8 -*-
"""Second fixture plugin used to exercise multi-plugin scenarios."""
from __future__ import annotations


def hello() -> str:
    return "hi from bar"


PLUGIN = {
    "name": "bar",
    "description": "Second test plugin",
    "version": "0.0.1",
}

TOOLS = [
    {
        "name": "hello",
        "handler": hello,
        "description": "Return a hello string",
        "tags": ["kind:read"],
    },
]
