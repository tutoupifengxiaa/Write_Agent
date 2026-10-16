TRANSIENT_ERROR = "transient_error"
BUSINESS_ERROR = "business_error"
PLATFORM_ERROR = "platform_error"

ERROR_RETRY_LIMITS = {
    TRANSIENT_ERROR: 2,
    BUSINESS_ERROR: 0,
    PLATFORM_ERROR: 3,
}


def classify_error(error_code: str) -> str:
    if error_code.startswith("timeout") or error_code.startswith("network"):
        return TRANSIENT_ERROR
    if error_code.startswith("platform"):
        return PLATFORM_ERROR
    return BUSINESS_ERROR


def max_retries(error_type: str) -> int:
    if error_type not in ERROR_RETRY_LIMITS:
        raise ValueError(f"unknown error type: {error_type}")
    return ERROR_RETRY_LIMITS[error_type]
