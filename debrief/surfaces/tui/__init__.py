"""The full-screen terminal app."""


def run_app() -> None:
    from debrief.surfaces.tui.app import DebriefApp

    DebriefApp().run()
