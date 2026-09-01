def is_token_valid(expiry, current_time):
    return expiry < current_time
