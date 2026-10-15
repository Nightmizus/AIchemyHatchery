"""Certbot-only bootstrap, installed in /usr/local/lib/hatchery-certbot.

Changing acme.client.DEFAULT_NETWORK_TIMEOUT after import does not change the
default already bound to ClientNetwork.__init__. Supply the actual argument.
"""
from functools import wraps
import inspect

try:
    from acme import client
except ImportError:
    client = None

if client is not None:
    original_init = client.ClientNetwork.__init__
    init_signature = inspect.signature(original_init)

    @wraps(original_init)
    def init_with_network_timeout(*args, **kwargs):
        bound = init_signature.bind(*args, **kwargs)
        bound.arguments.setdefault('timeout', 180)
        return original_init(*bound.args, **bound.kwargs)

    client.ClientNetwork.__init__ = init_with_network_timeout
