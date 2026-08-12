"""Qt Quick user interface for MaoMao."""


def main() -> int:
    from .app import main as run

    return run()


__all__ = ["main"]
