"""CLI-only entry points; importing the plugin starts no probes or workers."""


def _monitor(_args):
    from .monitor import main

    return main()


def _queue(_args):
    from .queue_diagnostic import main

    return main()


_COMMANDS = {"monitor": _monitor, "queue": _queue}


def _setup(parser):
    actions = parser.add_subparsers(dest="honcho_ops_action", required=True)
    actions.add_parser("monitor", help="Check pipeline health; healthy runs stay silent")
    actions.add_parser("queue", help="Inspect queue work units without modifying Honcho")


def _run(args):
    return _COMMANDS[args.honcho_ops_action](args)


def register(ctx):
    ctx.register_cli_command(
        name="honcho-ops",
        help="Monitor Honcho and inspect its work queue",
        setup_fn=_setup,
        handler_fn=_run,
    )
