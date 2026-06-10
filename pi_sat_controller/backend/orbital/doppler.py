SPEED_OF_LIGHT_M_S = 299_792_458


def doppler_shift_hz(frequency_hz: int, range_rate_m_s: float) -> int:
    """Return Doppler shift for a frequency and line-of-sight range rate.

    Positive range rate means the satellite is moving away from the observer.
    The observed frequency is lower in that case, so the shift is negative.
    """
    return round(-frequency_hz * range_rate_m_s / SPEED_OF_LIGHT_M_S)

