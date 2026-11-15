"""The Lambda entry points load with only what the bundle carries, and the API stays light."""

from __future__ import annotations

import subprocess
import sys

from cloud.market_service import api_handler, poller_handler


def test_both_entry_points_expose_a_handler() -> None:
    assert callable(api_handler.lambda_handler)
    assert callable(poller_handler.lambda_handler)


def test_the_api_never_loads_the_pollers_libraries_or_aws_until_it_needs_them() -> None:
    code = (
        "import sys, cloud.market_service.api_handler;"
        "heavy = {'boto3', 'cryptography', 'websockets', 'statsapi'} & set(sys.modules);"
        "sys.exit(f'loaded {heavy}' if heavy else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_the_poller_loads_its_native_libraries_only_when_a_key_calls_for_them() -> None:
    code = (
        "import sys, cloud.market_service.poller_handler;"
        "heavy = {'boto3', 'cryptography', 'websockets'} & set(sys.modules);"
        "sys.exit(f'loaded {heavy}' if heavy else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
