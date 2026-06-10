def tx_allowed(elevation_deg: float, cat_connected: bool, valid_pass: bool) -> bool:
    return elevation_deg >= 0 and cat_connected and valid_pass

