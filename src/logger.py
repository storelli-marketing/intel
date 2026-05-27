"""Shared logger: writes to stdout and data/run.log."""
import logging
import os

_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "run.log")


def get_logger(name: str = "storelli") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        file_handler = logging.FileHandler(_LOG_PATH)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError:
        pass

    return logger
