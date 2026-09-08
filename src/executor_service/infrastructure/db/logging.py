"""Keep database values and driver DETAIL text out of exception logs."""

import logging

from sqlalchemy.exc import SQLAlchemyError


def _database_error(error: BaseException | None) -> SQLAlchemyError | None:
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, SQLAlchemyError):
            return error
        error = error.__cause__ or error.__context__
    return None


def _summary(error: SQLAlchemyError) -> str:
    original = getattr(error, "orig", None)
    sqlstate = getattr(original, "sqlstate", None)
    code = (
        sqlstate
        if isinstance(sqlstate, str)
        and len(sqlstate) == 5
        and sqlstate.isalnum()
        else "unknown"
    )
    return (
        f"Database operation failed; error_type={type(error).__name__} "
        f"sqlstate={code}. SQL and parameter values omitted."
    )


class DatabaseErrorFilter(logging.Filter):
    """Preserve traceback locations, but never format raw DB exception text."""

    def filter(self, record: logging.LogRecord) -> bool:
        error = (
            _database_error(record.exc_info[1]) if record.exc_info else None
        )
        if error is not None:
            summary = _summary(error)
            record.msg = summary
            record.args = ()
            assert record.exc_info is not None
            record.exc_info = (
                RuntimeError,
                RuntimeError(summary),
                record.exc_info[2],
            )
            record.exc_text = None
        elif isinstance(record.msg, SQLAlchemyError):
            record.msg = _summary(record.msg)
            record.args = ()
        elif isinstance(record.args, tuple):
            record.args = tuple(
                _summary(value)
                if isinstance(value, SQLAlchemyError)
                else value
                for value in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: _summary(value)
                if isinstance(value, SQLAlchemyError)
                else value
                for key, value in record.args.items()
            }
        return True


def install_database_error_filters() -> None:
    """Protect configured handlers without replacing deployment logging.

    Call after the deployment's logging initialization. If it creates new
    handlers later, call again; installation is idempotent per handler.
    """
    loggers = [logging.getLogger()]
    loggers.extend(
        logger
        for logger in logging.root.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    )
    for logger in loggers:
        for handler in logger.handlers:
            if not any(
                isinstance(item, DatabaseErrorFilter)
                for item in handler.filters
            ):
                handler.addFilter(DatabaseErrorFilter())
