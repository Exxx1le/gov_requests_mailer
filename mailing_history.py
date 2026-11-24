import errno
import json
import os
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep


HISTORY_FILE = Path(__file__).resolve().with_name("mailing_history.json")
STATUSES = {
    "pending": "отправка начата, результат не записан",
    "sent": "отправлено",
    "failed": "не отправлено",
    "uncertain": "результат требует проверки",
}
_FIELDS = {"start_date", "end_date", "timestamp", "status"}


class HistoryError(ValueError):
    """The existing history cannot be used safely."""


@contextmanager
def mailing_lock():
    """Serialize the entire check, generation, send and cleanup across processes."""
    with HISTORY_FILE.with_suffix(".json.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            while True:
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    sleep(0.1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _validate_attempt(attempt):
    if not isinstance(attempt, dict) or attempt.keys() != _FIELDS:
        raise ValueError
    if not all(isinstance(value, str) for value in attempt.values()):
        raise ValueError
    start = date.fromisoformat(attempt["start_date"])
    end = date.fromisoformat(attempt["end_date"])
    timestamp = datetime.fromisoformat(attempt["timestamp"])
    if (
        start.isoformat() != attempt["start_date"]
        or end.isoformat() != attempt["end_date"]
        or start > end
        or timestamp.utcoffset() is None
        or attempt["status"] not in STATUSES
    ):
        raise ValueError
    timestamp.astimezone(timezone.utc)


def load_history():
    try:
        with HISTORY_FILE.open(encoding="utf-8") as source:
            attempts = json.load(source)
        if not isinstance(attempts, list):
            raise ValueError
        for attempt in attempts:
            _validate_attempt(attempt)
    except FileNotFoundError:
        if not os.path.lexists(HISTORY_FILE):
            return []
        raise
    except (ValueError, OverflowError, RecursionError):
        raise HistoryError("История повреждена. Проверьте mailing_history.json вручную.") from None
    return attempts


def save_history(attempts):
    """Replace the history atomically. Call only while holding mailing_lock()."""
    temporary_path = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=HISTORY_FILE.parent,
            prefix=".mailing_history.json.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(attempts, temporary, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, HISTORY_FILE)
        if os.name == "posix":
            directory = os.open(HISTORY_FILE.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def new_attempt(start_date, end_date):
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "pending",
    }


def last_blocking_status(attempts, start_date, end_date):
    start, end = start_date.isoformat(), end_date.isoformat()
    for attempt in reversed(attempts):
        if (
            attempt["start_date"] == start
            and attempt["end_date"] == end
            and attempt["status"] != "failed"
        ):
            return attempt["status"]
    return None


def print_history(attempts):
    if not attempts:
        print("История рассылок пуста.")
    for attempt in attempts:
        timestamp = datetime.fromisoformat(attempt["timestamp"]).astimezone(timezone.utc)
        print(
            f'{attempt["start_date"]} — {attempt["end_date"]} | '
            f'{timestamp:%d.%m.%Y %H:%M:%S} UTC | {STATUSES[attempt["status"]]}'
        )
