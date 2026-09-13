"""pytest 共享 fixture：把 server/ 加到 sys.path，保证 `import gateway.x` 可用。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
