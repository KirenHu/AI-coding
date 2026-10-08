"""Entry point for an OS-native wrapper. WorkTwin is a loopback service + UI."""
import multiprocessing
import sys

from worktwin.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.argv = [sys.argv[0], "serve", "--open"]
    main()
