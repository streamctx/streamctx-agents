"""
sample_target/token_utils.py
A tiny standalone module (NOT the real streamctx package) used purely
to give the Coding Agent a safe, realistic bug to practice fixing.
"""

def compression_ratio(original_tokens, compressed_tokens):
    """
    Returns the percentage of tokens saved by compression.
    e.g. original=100, compressed=40 -> 60.0 (60% saved)
    """
    if original_tokens == 0:
        return 0.0
    
    saved_ratio = (original_tokens - compressed_tokens) / original_tokens
    return saved_ratio * 100


def average_savings(sessions):
    """
    sessions: list of (original_tokens, compressed_tokens) tuples
    Returns the average compression_ratio across all sessions.
    """
    if not sessions:
        return 0.0
        
    total = 0
    for original, compressed in sessions:
        total += compression_ratio(original, compressed)
    return total / len(sessions)


def lookup_token(session_tokens, key):
    """Look up a stored token count. Missing keys should return 0, not KeyError."""
    return session_tokens[key]
