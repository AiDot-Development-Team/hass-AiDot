"""Make the integration's modules importable standalone.

``ble_gateway_client`` only depends on the stdlib and the installed ``aidot``
library (no relative imports), so we put the integration dir on ``sys.path``
and import the module directly — avoiding the package ``__init__`` which would
pull in Home Assistant.
"""

import sys
from pathlib import Path

_AIDOT_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "aidot"
sys.path.insert(0, str(_AIDOT_DIR))
