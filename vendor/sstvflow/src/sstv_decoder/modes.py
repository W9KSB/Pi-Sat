"""Supported analog mode layouts, in protocol seconds.

Robot24 is the 160x120, VIS4 full-chroma mode, not Robot24 monochrome.
Its video allocation uses two units for Y and one each for Cr and Cb.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Mode:
    vis: int
    width: int
    height: int
    period: float
    sync: float
    family: str
    channels: tuple = ()
    markers: tuple = ()


MODES = {
    "Robot36": Mode(8, 320, 240, .150, .009, "robot36"),
    "PD120": Mode(95, 640, 496, .508480, .020, "pd"),
    "Robot72": Mode(12, 320, 240, .300, .009, "robot",
                    (("y", .012, .138), ("cr", .156, .069), ("cb", .231, .069)),
                    ((.1505, .154, 1500), (.2255, .229, 2300))),
    "Robot24": Mode(4, 160, 120, .2000125, .006, "robot",
                    (("y", .0072, .09260625), ("cr", .10360625, .046303125),
                     ("cb", .153709375, .046303125)),
                    ((.1001, .1019, 1500), (.1502, .1520, 2300))),
    "MartinM1": Mode(44, 320, 256, .446446, .004862, "rgb",
                     (("g", .005434, .146432), ("b", .152438, .146432), ("r", .299442, .146432))),
    "MartinM2": Mode(40, 320, 256, .226798, .004862, "rgb",
                     (("g", .005434, .073216), ("b", .079222, .073216), ("r", .153010, .073216))),
    "PD180": Mode(96, 640, 496, .754240, .020, "pd"),
}
SUPPORTED_MODES = tuple(MODES)
VIS_MODES = {profile.vis: name for name, profile in MODES.items()}


def sync_matches(profile, duration):
    if profile.family == "rgb":
        return .002 <= duration <= .007
    if profile.sync == .006:
        return .004 <= duration <= .009
    if profile.sync == .009:
        return .006 <= duration <= .013
    return .016 <= duration <= .023
