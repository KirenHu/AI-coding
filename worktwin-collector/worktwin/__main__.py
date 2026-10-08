"""Python -m worktwin entrypoint; native wrappers import worktwin.cli instead."""

from .cli import _configure_frozen_stdio, main


if __name__ == "__main__":
    main()
