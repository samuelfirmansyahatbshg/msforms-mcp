class FormsError(RuntimeError):
    """Safe, actionable error; never include tokens or response bodies."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class CapabilityUnavailable(FormsError):
    def __init__(self, capability: str):
        super().__init__(
            "capability_unavailable", f"Not yet measured: {capability}. See MEASUREMENTS.md."
        )
