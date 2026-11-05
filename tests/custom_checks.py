"""Example custom (python) checks, used by the tests."""

import re


def mentions_order_number(response, case):
    return (True, "") if re.search(r"#BC-\d+", response) else (False, "no order number")


def explodes(response, case):
    return 1 / 0
