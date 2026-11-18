class PhotoError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class StorageFull(PhotoError):
    def __init__(self, message: str):
        super().__init__(message, 507)
