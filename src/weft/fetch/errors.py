class FetchError(Exception):
    """A fetch that produced no usable response. `code` is stable and safe to branch on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
