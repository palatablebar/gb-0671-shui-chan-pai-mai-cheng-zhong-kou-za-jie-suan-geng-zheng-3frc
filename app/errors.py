class BusinessError(Exception):
    def __init__(self, code, message, status=422, data=None):
        self.code, self.message, self.status = code, message, status
        self.data = data
        super().__init__(message)

def require(condition, code, message):
    if not condition:
        raise BusinessError(code, message)
