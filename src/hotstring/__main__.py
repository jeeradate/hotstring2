"""``python -m hotstring`` entry point."""

from __future__ import annotations

from .app import run


def main() -> None:
    """Run HotString and exit with its status code."""

    raise SystemExit(run())


if __name__ == "__main__":
    main()
