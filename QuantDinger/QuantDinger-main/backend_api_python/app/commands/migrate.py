"""Fail-fast database migration entrypoint for deployments."""

from __future__ import annotations


def main() -> None:
    from app.utils.db import init_database

    init_database(strict_migrations=True)


if __name__ == "__main__":
    main()
