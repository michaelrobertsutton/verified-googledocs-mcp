"""Run the server via ``python -m verified_googledocs_mcp``.

Delegates to the same entry point as the ``verified-googledocs-mcp`` console
script, so ``python -m verified_googledocs_mcp`` and
``python -m verified_googledocs_mcp auth`` behave identically to the installed
command.
"""

from .server import main

if __name__ == "__main__":
    main()
