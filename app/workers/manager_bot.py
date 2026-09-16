"""Explicit manager-only entrypoint."""
from app.bot.runtime import main

if __name__ == "__main__":
    main("manager")
