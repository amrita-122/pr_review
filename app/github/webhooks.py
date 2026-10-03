import hashlib
import hmac


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """
    Verify the signature of a GitHub webhook request.

    Args:
        secret (str): The webhook secret configured in GitHub.
        body (bytes): The raw request body.
        header (str | None): The value of the 'X-Hub-Signature-256' header from the request.

    Returns:
        bool: True if the signature is valid, False otherwise.
    """

    if header is None:
        return False

    # GitHub sends the signature in the format: sha256=signature
    try:
        sha_name, signature = header.split("=")
    except ValueError:
        return False

    if sha_name != "sha256":
        return False

    # Create a new HMAC object using the secret and the request body
    mac = hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha256)

    # Compare the computed HMAC with the signature from the header
    return hmac.compare_digest(mac.hexdigest(), signature)
