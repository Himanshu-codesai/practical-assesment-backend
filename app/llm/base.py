"""Prompt templates. Each task has one file in prompts/ with a system part and a user part."""
from functools import lru_cache
from pathlib import Path
from string import Template

PROMPT_DIR = Path(__file__).parent / "prompts"
_SEPARATOR = "---USER---"


@lru_cache
def _load(name: str) -> tuple[str, Template]:
    system, user = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8").split(_SEPARATOR)
    return system.strip(), Template(user.strip())


def render_prompt(name: str, **values) -> tuple[str, str]:
    """Return (system, user) for prompts/<name>.md with $placeholders filled in.

    Prompts live in files, not in code, so they can be reviewed and changed on their own.
    The system part is constant per task and the data goes in the user part.
    """
    system, user = _load(name)
    return system, user.substitute(**values)
