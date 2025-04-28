import logging
import random


def one_liner(s: str, cut_len: int | None = None) -> str:
    s = s.replace("\n", " ")
    while "  " in s:
        s = s.replace("  ", " ")
    return s[:cut_len] if cut_len else s


def percent_chance(percent: float) -> bool:
    if percent < 0.0 or percent > 100.0:
        raise ValueError(f"`percent` should be between 0. an 100., not {percent}")
    chance = percent / 100.0
    return random.random() < chance


def get_logger(component_name: str, level=logging.DEBUG) -> logging.Logger:
    logger = logging.Logger(component_name)
    logger.setLevel(level)

    formatter = logging.Formatter(
        "[%(asctime)s][%(name)s][%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler_console = logging.StreamHandler()
    handler_console.setFormatter(formatter)
    handler_console.setLevel(level)
    logger.addHandler(handler_console)

    return logger
