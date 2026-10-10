def fail(message, status=400):
    from .store import RoomError
    raise RoomError(message, status)
