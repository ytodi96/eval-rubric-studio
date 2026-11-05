"""A stand-in for another project's Python function, used by the python-adapter tests."""


def reply(message, context):
    return {"draft": f"Echo: {message} ({context.get('order', '-')})", "usage": {"cost_usd": 0.001}}
