"""Allow `python -m leadforge` as a PATH-free fallback (see skills/generate-leads/references/troubleshooting.md)."""

from leadforge.cli import main

if __name__ == "__main__":
    main()
