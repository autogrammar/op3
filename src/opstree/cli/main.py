"""CLI entry point for op3."""

from __future__ import annotations
import click
from opstree import __version__
from opstree.cli.commands.scan import scan
from opstree.cli.commands.drift import drift
from opstree.cli.commands.convert import convert


@click.group()
@click.version_option(version=__version__)
def cli():
    try:
        from .autoupdate import check_for_updates
        check_for_updates("op3")
    except Exception:
        pass
    """op3 — Layered operations tree for infrastructure observation."""
    pass


cli.add_command(scan)
cli.add_command(drift)
cli.add_command(convert)


if __name__ == "__main__":
    cli()
