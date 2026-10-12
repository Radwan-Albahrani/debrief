"""The two ways a run ends early."""


class DebriefError(Exception):
    """Something the user can act on, such as a missing model or an unreadable file. The message is shown as is."""


class Cancelled(Exception):
    """The user cancelled a running job."""
