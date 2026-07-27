"""Unit tests for the process entry point: ``main()`` and its dispatch.

This is the only coverage of the actual process startup path — every other test
in this suite constructs ``mcp`` directly via FastMCP's in-memory ``Client`` and
never goes through ``main()``. These tests exist because ``main()`` is reachable
three ways (the ``verified-googledocs-mcp`` console script, ``python -m
verified_googledocs_mcp``, and ``uv run verified-googledocs-mcp``), and a
regression in the dispatch logic or in the FastMCP ``run()`` call contract could
break all three while the rest of the suite stays green. See issue #59.

All tests are synchronous — ``main()`` is sync and never starts a real
transport, since ``mcp.run`` is patched out everywhere it would otherwise be
reached.
"""

from __future__ import annotations

import inspect
import runpy
import sys
from importlib.metadata import entry_points
from unittest.mock import patch

import verified_googledocs_mcp.server as server
from verified_googledocs_mcp.server import main, mcp


class TestMainDispatch:
    """Branch coverage for ``main()``'s argv handling.

    Every test sets ``sys.argv`` explicitly. Under pytest, ``sys.argv[1]`` is
    already a test path, so ``len(sys.argv) > 1`` is true before any patching —
    without an explicit argv, a "no args" test would pass for the wrong reason.

    ``mcp.run`` is patched with ``patch.object``, not a lenient
    ``monkeypatch.setattr(..., raising=False)``: a typo'd target under
    ``patch.object`` raises ``AttributeError`` immediately, whereas a lenient
    setattr would silently leave the real ``run()`` in place and the test would
    hang waiting on stdin instead of failing.
    """

    def test_no_args_starts_server(self, monkeypatch: object) -> None:
        monkeypatch.setattr(sys, "argv", ["verified-googledocs-mcp"])
        with (
            patch.object(server.mcp, "run") as mock_run,
            patch("verified_googledocs_mcp.auth.run_auth_flow") as mock_auth,
        ):
            main()
        mock_run.assert_called_once_with()
        mock_auth.assert_not_called()

    def test_auth_subcommand_runs_oauth_flow(self, monkeypatch: object) -> None:
        monkeypatch.setattr(sys, "argv", ["verified-googledocs-mcp", "auth"])
        with (
            patch.object(server.mcp, "run") as mock_run,
            patch("verified_googledocs_mcp.auth.run_auth_flow") as mock_auth,
        ):
            main()
        mock_auth.assert_called_once()
        mock_run.assert_not_called()

    def test_unrecognized_arg_falls_through_to_server(self, monkeypatch: object) -> None:
        monkeypatch.setattr(sys, "argv", ["verified-googledocs-mcp", "--http"])
        with (
            patch.object(server.mcp, "run") as mock_run,
            patch("verified_googledocs_mcp.auth.run_auth_flow") as mock_auth,
        ):
            main()
        mock_run.assert_called_once_with()
        mock_auth.assert_not_called()

    def test_auth_ignores_trailing_args(self, monkeypatch: object) -> None:
        monkeypatch.setattr(sys, "argv", ["verified-googledocs-mcp", "auth", "--force"])
        with (
            patch.object(server.mcp, "run") as mock_run,
            patch("verified_googledocs_mcp.auth.run_auth_flow") as mock_auth,
        ):
            main()
        mock_auth.assert_called_once()
        mock_run.assert_not_called()


class TestRunContract:
    """Introspection guard against the unbounded ``fastmcp>=3.0`` floor.

    ``pyproject.toml`` pins ``fastmcp>=3.0`` with no upper ceiling, so
    ``mcp.run()`` is exactly the call a future FastMCP major version could
    break. Mock-based dispatch tests alone would not catch that — a
    ``MagicMock`` accepts any call shape — so these tests introspect the real,
    installed ``FastMCP.run`` instead of a mock.
    """

    def test_run_accepts_zero_arguments(self) -> None:
        """``main()`` calls ``mcp.run()`` bare; that call must still bind."""
        inspect.signature(type(mcp).run).bind(mcp)

    def test_run_defaults_to_implicit_transport(self) -> None:
        """A bare call relies on ``transport`` defaulting to stdio-equivalent.

        If a future FastMCP changed this default, every MCP client would break
        while the mocked dispatch tests above stayed green.
        """
        transport_param = inspect.signature(type(mcp).run).parameters["transport"]
        assert transport_param.default is None

    def test_run_is_synchronous(self) -> None:
        """If ``run()`` became a coroutine, ``main()`` would return an
        un-awaited coroutine and exit immediately — a mock would not notice.
        """
        assert not inspect.iscoroutinefunction(type(mcp).run)


class TestEntryPointsReachMain:
    """Backs the issue's "reachable three ways" claim with real reachability
    checks, not just declarations.
    """

    def test_module_execution_reaches_main(self, monkeypatch: object) -> None:
        """``python -m verified_googledocs_mcp`` must actually reach ``main()``.

        An identity check (``__main__.main is server.main``) would not catch a
        regression here: it stays true even if the
        ``if __name__ == "__main__": main()`` guard in ``__main__.py`` were
        deleted. ``runpy.run_module`` re-executes the module for real.

        This relies on ``verified_googledocs_mcp.server`` already being
        imported at module scope (above) before ``run_module`` runs:
        ``__main__.py``'s ``from .server import main`` resolves through
        ``sys.modules``, so the patch on ``server.mcp.run`` only lands because
        ``server`` in this test module and the one ``__main__`` imports are the
        same object.
        """
        monkeypatch.setattr(sys, "argv", ["verified-googledocs-mcp"])
        with patch.object(server.mcp, "run") as mock_run:
            runpy.run_module("verified_googledocs_mcp", run_name="__main__")
        mock_run.assert_called_once_with()

    def test_console_script_resolves_to_main(self) -> None:
        """The installed console script must point at ``server:main``.

        Checks the installed distribution's metadata, not just the
        ``pyproject.toml`` declaration, so a packaging change that builds a
        console script pointing somewhere else would be caught here.
        """
        scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts")}
        assert scripts.get("verified-googledocs-mcp") == "verified_googledocs_mcp.server:main"
