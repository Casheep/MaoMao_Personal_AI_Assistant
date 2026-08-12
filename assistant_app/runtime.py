from __future__ import annotations

import threading
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .agent import PersonalAgent
    from .database import Database
    from .tts import SpeechSynthesizer

from .performance import timed


@timed("runtime.build")
def build_agent(
    config: dict[str, Any],
    session_id: str,
    confirmation_callback: Callable[[str, dict[str, Any]], bool],
    progress_callback: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    include_tts: bool = True,
) -> tuple[PersonalAgent, Database, SpeechSynthesizer | None]:
    """Build the conversation runtime only when a prompt actually needs it."""
    from .agent import PersonalAgent
    from .budget import BudgetManager
    from .config import app_paths
    from .database import Database
    from .providers import HybridModelClient, KimiClient, MiMoClient
    from .secrets import load_kimi_key, load_mimo_key
    from .tools import ToolRegistry
    from .tts import SpeechSynthesizer

    paths = app_paths()
    database = Database(paths.database)
    budget = BudgetManager(database, config["api"])
    kimi_client = KimiClient(
        load_kimi_key(paths.key_file), config, database, budget, session_id
    )
    mimo_client = MiMoClient(
        load_mimo_key(paths.key_file), config, database, budget, session_id
    )
    client = HybridModelClient(kimi_client, mimo_client, config)
    tools = ToolRegistry(
        paths,
        database,
        session_id,
        confirmation_callback,
        application_config=config.get("applications", {}),
        smart_home_config=config.get("smart_home", {}),
        automation_config=config.get("automation", {}),
        skills_config=config.get("skills", {}),
        cancel_event=cancel_event,
    )
    agent = PersonalAgent(
        config,
        database,
        client,
        tools,
        session_id,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )
    tts = SpeechSynthesizer(config["tts"]) if include_tts else None
    return agent, database, tts
