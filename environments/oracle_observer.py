"""Candidate-side observation only. Expected values and verdicts stay on the controller.

Every authoritative case gets a new disposable container. Candidate output is
untrusted: the external controller requires one nonce-bound, bounded observation.
"""

import json
import pathlib
import sys
import unittest

input_data = json.loads(pathlib.Path("/workspace/__evaluator_input.json").read_text())
nonce = input_data["nonce"]
scope = {"__name__": "__evaluator_probe__"}
sys.path.insert(0, "/workspace")
try:
    exec(compile(input_data["probe"], "<authority-observation>", "exec"), scope)
    record = {"nonce": nonce, "kind": "value", "value": scope["value"]}
except unittest.SkipTest:
    record = {"nonce": nonce, "kind": "skipped"}
except BaseException as error:
    record = {"nonce": nonce, "kind": "exception", "exception": type(error).__name__}
print(json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
