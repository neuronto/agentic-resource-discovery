"""Delegate the CLI to ard_publish, so there is one implementation."""
from ard_publish.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
