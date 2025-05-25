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
