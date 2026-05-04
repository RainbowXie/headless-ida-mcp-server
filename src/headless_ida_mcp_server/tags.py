# -*- coding: utf-8 -*-
"""Capability tag table for every MCP tool / resource the server registers.

Three-tier capability classification, ported from the Ramune-ida plugin
framework (`https://github.com/RamuneIDA/Ramune-ida`,
`docs/writing-plugins.md`) and adapted to this fork's wrapper layer.

Tier semantics
--------------

Each registered tool MUST have **exactly one** ``kind:*`` tag. The three
tiers are mutually exclusive:

* ``kind:read`` -- the tool does not mutate IDB state. Safe to call in any
  read-only deployment. The wrapper does NOT create an undo point.
* ``kind:write`` -- the tool mutates IDB state, and IDA's
  ``ida_undo.perform_undo()`` can roll the change back. The wrapper
  automatically calls ``ida_undo.create_undo_point(name, name)`` before
  invoking the underlying function so a subsequent ``undo()`` MCP call
  reverts the side effect.
* ``kind:unsafe`` -- the tool is destructive or irreversible from
  ``ida_undo``'s perspective: it closes the IDB, mutates raw bytes that
  may straddle instruction boundaries, runs arbitrary Python that can
  touch the filesystem / network, or modifies state outside the IDB
  (e.g. debuggee process memory). The wrapper deliberately does NOT
  create an undo point because ``ida_undo`` cannot recover from these
  mutations; the caller is expected to apply extra caution (or exclude
  the tool entirely in unattended mode via ``--exclude-tags``).

Optional secondary tags use the ``core::`` / ``ext::`` namespaces and
form a group path that ``--exclude-tags`` can match with a ``fnmatch``
glob (e.g. ``core::debug::*`` to drop every ``dbg_*`` tool). These are
purely informational for the filter; the wrapper logic only consumes
``kind:*``.

Upstream sync workflow
----------------------

The vendored tool surface in ``src/headless_ida_mcp_server/ida_mcp/`` is
re-synced from ``mrexodia/ida-pro-mcp`` periodically. After every sync,
maintainers MUST audit ``TOOL_TAGS`` below: every tool name appearing in
``MCP_TOOLS`` that is not already enumerated here defaults to
``["kind:read"]`` (conservative -- a missing tag won't accidentally let
an unknown writer skip its undo point silently, but the missing tag also
WON'T trigger auto-undo for a real write tool). The server emits a
``WARNING`` log line listing such untagged tools at startup so the gap
is visible. See ``docs/agent-quickstart.md`` "Resync workflow".

Vendored breakdown (81 entries)
-------------------------------

* ``kind:read`` x64: every pure query tool, including all 20 ``dbg_*``
  tools (they read register / breakpoint state of an external debug
  session that idalib does not host by default; ``dbg_write`` mutates
  debuggee process memory but the IDB itself is untouched, so it stays
  ``kind:read`` w.r.t. IDB recovery semantics).
* ``kind:write`` x14: write IDB metadata that ``ida_undo`` can revert.
  ``diff_before_after`` is conservatively classified write because it
  stores a baseline that subsequent calls compare against; later
  refinement may downgrade it after empirical study.
* ``kind:unsafe`` x3: ``patch`` / ``patch_asm`` (raw byte writes that
  may straddle instruction boundaries) and ``undefine`` (clears
  instruction / data definitions; the re-disassembly path after an
  ``ida_undo`` may not be exact).

Fork-only entries (4 -- ``set_binary_path`` / ``unset`` / ``py_eval`` /
``undo``) live in ``server.py`` directly and are listed here so the
filter and undo logic apply uniformly.
"""
from __future__ import annotations

import fnmatch
from typing import Dict, List

# Mapping every registered MCP tool to its tag list. Ordering: fork-only
# entries first (small set, easy to scan), then vendored entries grouped
# by source file in the same order ``MCP_TOOLS`` enumerates them.
TOOL_TAGS: Dict[str, List[str]] = {
    # ------------------------------------------------------------------
    # Fork-only lifecycle / extension tools (defined in server.py)
    # ------------------------------------------------------------------
    # set_binary_path opens / mutates an IDB. It is recoverable in the
    # sense that the user can re-open the original IDB; tag it kind:write
    # so it is surfaced in normal write filtering.
    "set_binary_path": ["kind:write", "core::lifecycle"],
    # unset closes the IDB. ida_undo cannot recover a closed session.
    "unset": ["kind:unsafe", "core::lifecycle"],
    # py_eval runs arbitrary Python: filesystem, network, sys.path,
    # os.unlink(IDB_PATH) etc. ida_undo cannot recover any of that.
    "py_eval": ["kind:unsafe", "ext::py_eval"],
    # undo itself drives ida_undo.perform_undo(). Tagging it kind:read
    # avoids recursion (we do NOT want auto-undo around the undo tool)
    # and keeps it usable in read-only mode (an agent can roll back
    # earlier writes even after the writer surface has been excluded).
    "undo": ["kind:read", "core::undo"],

    # ------------------------------------------------------------------
    # Vendored: api_analysis.py (14 tools, all read)
    # ------------------------------------------------------------------
    "analyze_batch": ["kind:read", "core::analysis::batch"],
    "basic_blocks": ["kind:read", "core::analysis::cfg"],
    "callees": ["kind:read", "core::analysis::cfg"],
    "callgraph": ["kind:read", "core::analysis::cfg"],
    "decompile": ["kind:read", "core::analysis::decompile"],
    "disasm": ["kind:read", "core::analysis::disasm"],
    "export_funcs": ["kind:read", "core::analysis::export"],
    "find": ["kind:read", "core::analysis::search"],
    "find_bytes": ["kind:read", "core::analysis::search"],
    "func_profile": ["kind:read", "core::analysis::profile"],
    "insn_query": ["kind:read", "core::analysis::query"],
    "xref_query": ["kind:read", "core::analysis::xref"],
    "xrefs_to": ["kind:read", "core::analysis::xref"],
    "xrefs_to_field": ["kind:read", "core::analysis::xref"],

    # ------------------------------------------------------------------
    # Vendored: api_composite.py (4 tools)
    # ------------------------------------------------------------------
    "analyze_component": ["kind:read", "core::analysis::composite"],
    "analyze_function": ["kind:read", "core::analysis::composite"],
    # diff_before_after stores a baseline on first run; later calls
    # mutate that stored baseline. Conservative kind:write.
    "diff_before_after": ["kind:write", "core::analysis::composite"],
    "trace_data_flow": ["kind:read", "core::analysis::composite"],

    # ------------------------------------------------------------------
    # Vendored: api_core.py (13 tools)
    # ------------------------------------------------------------------
    "entity_query": ["kind:read", "core::query"],
    "find_regex": ["kind:read", "core::search"],
    "func_query": ["kind:read", "core::query"],
    # idb_save persists the IDB to disk; tag write so a read-only mode
    # cannot inadvertently overwrite an on-disk database.
    "idb_save": ["kind:write", "core::lifecycle::save"],
    "imports": ["kind:read", "core::query"],
    "imports_query": ["kind:read", "core::query"],
    "int_convert": ["kind:read", "core::util"],
    "list_funcs": ["kind:read", "core::query"],
    "list_globals": ["kind:read", "core::query"],
    "lookup_funcs": ["kind:read", "core::query"],
    "search_text": ["kind:read", "core::search"],
    "server_health": ["kind:read", "core::server"],
    "server_warmup": ["kind:read", "core::server"],

    # ------------------------------------------------------------------
    # Vendored: api_debug.py (20 tools)
    # ------------------------------------------------------------------
    # All dbg_* tools target a live debug session that idalib does not
    # host by default; they read debuggee state without mutating the IDB
    # database. dbg_write modifies debuggee memory but the IDB itself is
    # untouched, so it stays kind:read w.r.t. IDB recovery semantics.
    # The shared core::debug::* group lets `--exclude-tags` drop the
    # entire surface in deployments that never run a debugger.
    "dbg_add_bp": ["kind:read", "core::debug::breakpoints"],
    "dbg_bps": ["kind:read", "core::debug::breakpoints"],
    "dbg_continue": ["kind:read", "core::debug::session"],
    "dbg_delete_bp": ["kind:read", "core::debug::breakpoints"],
    "dbg_exit": ["kind:read", "core::debug::session"],
    "dbg_gpregs": ["kind:read", "core::debug::registers"],
    "dbg_gpregs_remote": ["kind:read", "core::debug::registers"],
    "dbg_read": ["kind:read", "core::debug::memory"],
    "dbg_regs": ["kind:read", "core::debug::registers"],
    "dbg_regs_all": ["kind:read", "core::debug::registers"],
    "dbg_regs_named": ["kind:read", "core::debug::registers"],
    "dbg_regs_named_remote": ["kind:read", "core::debug::registers"],
    "dbg_regs_remote": ["kind:read", "core::debug::registers"],
    "dbg_run_to": ["kind:read", "core::debug::session"],
    "dbg_stacktrace": ["kind:read", "core::debug::session"],
    "dbg_start": ["kind:read", "core::debug::session"],
    "dbg_step_into": ["kind:read", "core::debug::session"],
    "dbg_step_over": ["kind:read", "core::debug::session"],
    "dbg_toggle_bp": ["kind:read", "core::debug::breakpoints"],
    # dbg_write mutates debuggee process memory; IDB state untouched.
    "dbg_write": ["kind:read", "core::debug::memory"],

    # ------------------------------------------------------------------
    # Vendored: api_memory.py (6 tools)
    # ------------------------------------------------------------------
    "get_bytes": ["kind:read", "core::memory::read"],
    "get_global_value": ["kind:read", "core::memory::read"],
    "get_int": ["kind:read", "core::memory::read"],
    "get_string": ["kind:read", "core::memory::read"],
    # patch writes raw bytes; ida_undo does not symmetrically restore
    # this kind of low-level mutation in every IDA build.
    "patch": ["kind:unsafe", "core::memory::write"],
    "put_int": ["kind:write", "core::memory::write"],

    # ------------------------------------------------------------------
    # Vendored: api_modify.py (7 tools)
    # ------------------------------------------------------------------
    "append_comments": ["kind:write", "core::modify::comments"],
    "define_code": ["kind:write", "core::modify::code"],
    "define_func": ["kind:write", "core::modify::code"],
    # patch_asm rewrites raw bytes via assemble(); the new bytes can
    # straddle the original instruction boundary, leaving a half-decoded
    # remainder. Mark unsafe -- ida_undo cannot guarantee a clean restore.
    "patch_asm": ["kind:unsafe", "core::modify::code"],
    "rename": ["kind:write", "core::modify::names"],
    "set_comments": ["kind:write", "core::modify::comments"],
    # undefine clears instruction / data definitions; the
    # re-disassembly path after an undo may not match the prior layout.
    "undefine": ["kind:unsafe", "core::modify::code"],

    # ------------------------------------------------------------------
    # Vendored: api_sigmaker.py (4 tools, all read)
    # ------------------------------------------------------------------
    "find_xref_signatures": ["kind:read", "core::sigmaker"],
    "make_signature": ["kind:read", "core::sigmaker"],
    "make_signature_for_function": ["kind:read", "core::sigmaker"],
    "make_signature_for_range": ["kind:read", "core::sigmaker"],

    # ------------------------------------------------------------------
    # Vendored: api_stack.py (3 tools)
    # ------------------------------------------------------------------
    "declare_stack": ["kind:write", "core::stack"],
    "delete_stack": ["kind:write", "core::stack"],
    "stack_frame": ["kind:read", "core::stack"],

    # ------------------------------------------------------------------
    # Vendored: api_survey.py (1 tool)
    # ------------------------------------------------------------------
    "survey_binary": ["kind:read", "core::survey"],

    # ------------------------------------------------------------------
    # Vendored: api_types.py (9 tools)
    # ------------------------------------------------------------------
    "declare_type": ["kind:write", "core::types"],
    "enum_upsert": ["kind:write", "core::types"],
    # infer_types only returns inferred types; it does not apply them
    # (set_type / type_apply_batch is the writer pair).
    "infer_types": ["kind:read", "core::types"],
    "read_struct": ["kind:read", "core::types"],
    "search_structs": ["kind:read", "core::types"],
    "set_type": ["kind:write", "core::types"],
    "type_apply_batch": ["kind:write", "core::types"],
    "type_inspect": ["kind:read", "core::types"],
    "type_query": ["kind:read", "core::types"],
}


# Default tags applied when a tool name is not in TOOL_TAGS. The spec
# mandates a conservative kind:read so an unknown name does not silently
# trigger auto-undo for something that may not even be a writer.
_DEFAULT_TAGS: List[str] = ["kind:read"]


# Names that are always reachable, even when ``--exclude-tags`` would
# otherwise drop them. The plugin contract mandates that the four
# discovery / lifecycle meta tools (``plugins`` / ``plugin_tools`` /
# ``enable_plugin`` / ``disable_plugin``) survive every filter so an agent
# can always discover and enable plugins. See ``mcp-capability-tags``
# Requirement: meta-tools allowlist immune to --exclude-tags.
META_TOOL_ALLOWLIST: set[str] = {
    "plugins",
    "plugin_tools",
    "enable_plugin",
    "disable_plugin",
}


# Snapshot of the static fork-only tag table at module load. Plugin tag
# injection MUST refuse to register a name that already exists here so a
# plugin cannot accidentally shadow a fork built-in. This frozen view is
# used by :func:`register_tags` -- ``TOOL_TAGS`` itself is mutable so the
# untagged-tool warning logic operates on the merged table.
_BUILTIN_TAGS: Dict[str, List[str]] = {k: list(v) for k, v in TOOL_TAGS.items()}


def builtin_tag_names() -> set[str]:
    """Return the names known at module-load time (fork-only + vendored).

    Used by the plugin discovery pipeline to detect ``PLUGIN.name``
    collisions with built-in tools (per ``mcp-plugin-contract`` D4).
    """
    return set(_BUILTIN_TAGS.keys())


def register_tags(name: str, tags: List[str]) -> None:
    """Inject a plugin tool's tag list into ``TOOL_TAGS``.

    Validates:

    * ``name`` is not already present in ``TOOL_TAGS`` (collision).
    * ``name`` does not collide with a fork built-in (snapshot-based).
    * ``tags`` contains exactly one ``kind:*`` tag.

    Mutates the table in place. Subsequent ``tags_for(name)`` returns the
    injected list; the untagged-tool warning ignores ``name`` thereafter.
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"register_tags: name must be a non-empty string (got {name!r})")
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ValueError(
            f"register_tags: tags for {name!r} must be a list of strings"
        )
    kinds = [t for t in tags if t.startswith("kind:") and t in {
        "kind:read", "kind:write", "kind:unsafe"
    }]
    if len(kinds) != 1:
        raise ValueError(
            f"register_tags: {name!r} must carry exactly one of "
            "kind:read|kind:write|kind:unsafe "
            f"(got {kinds!r})"
        )
    if name in _BUILTIN_TAGS:
        raise ValueError(
            f"register_tags: {name!r} collides with a fork built-in tool"
        )
    if name in TOOL_TAGS:
        raise ValueError(
            f"register_tags: {name!r} already registered (duplicate plugin tool?)"
        )
    TOOL_TAGS[name] = list(tags)


def unregister_tags(name: str) -> None:
    """Remove a previously-injected plugin tool entry.

    Used by tests and (in principle) by future hot-reload paths. Removing
    a fork built-in entry is rejected to keep the static table intact.
    """
    if name in _BUILTIN_TAGS:
        raise ValueError(
            f"unregister_tags: {name!r} is a fork built-in; refusing to remove"
        )
    TOOL_TAGS.pop(name, None)


def tags_for(name: str) -> List[str]:
    """Return the tag list for ``name``.

    If ``name`` is not in ``TOOL_TAGS`` the conservative default
    ``["kind:read"]`` is returned. Callers that need to detect the
    "untagged" condition should compare the returned list identity (or
    membership) against ``TOOL_TAGS`` directly; this function never
    raises.
    """
    return list(TOOL_TAGS.get(name, _DEFAULT_TAGS))


def is_excluded(name: str, patterns: List[str]) -> bool:
    """Return True if any tag of ``name`` matches any glob in ``patterns``.

    ``patterns`` is a list of ``fnmatch`` globs (case-sensitive via
    ``fnmatch.fnmatchcase``). An empty / None ``patterns`` short-circuits
    to False so ``parse_exclude_patterns("")`` from an unset env var
    leaves the registration loop untouched.

    The four plugin-contract meta tools (see :data:`META_TOOL_ALLOWLIST`)
    are unconditionally returned as not excluded so an agent always has a
    path to discover and enable plugins, even under aggressive filters
    like ``--exclude-tags 'kind:*'``.
    """
    if name in META_TOOL_ALLOWLIST:
        return False
    if not patterns:
        return False
    tags = tags_for(name)
    for pattern in patterns:
        for tag in tags:
            if fnmatch.fnmatchcase(tag, pattern):
                return True
    return False


def parse_exclude_patterns(env_or_cli: str) -> List[str]:
    """Split a comma-separated pattern string into a clean glob list.

    Used by ``server.py`` to decode ``IDA_MCP_EXCLUDE_TAGS`` (set by
    ``__main__.py`` from the ``--exclude-tags`` CLI flag) at module load.
    Empty tokens (leading / trailing / consecutive commas) are dropped.
    """
    if not env_or_cli:
        return []
    return [token.strip() for token in env_or_cli.split(",") if token.strip()]
