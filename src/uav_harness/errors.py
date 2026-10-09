class HarnessError(Exception):
    """A known rejected request or observed failure."""


class Uncertain(HarnessError):
    """An action may have had effects; never replay automatically."""


class Rejected(HarnessError):
    def __init__(self, command, result):
        self.command, self.result = command, result
        super().__init__(f"command {command} rejected with MAV_RESULT={result}")

