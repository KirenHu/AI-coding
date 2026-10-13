"""Keep a claimed background operation live while model calls are in progress."""
import threading
from contextlib import contextmanager


@contextmanager
def renew_lease(db, statement, parameters, *, interval=60):
    stopped=threading.Event()
    def renew():
        while not stopped.wait(interval):
            with db.connect() as con:
                if not con.execute(statement,parameters).rowcount:
                    return
    thread=threading.Thread(target=renew,name='worktwin-lease',daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()
