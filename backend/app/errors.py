class ServiceError(Exception):
    def __init__(self, code, message, status=409, errors=None):
        self.code, self.message, self.status = code, message, status
        self.errors = errors or []
