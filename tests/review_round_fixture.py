"""Independent reservation fixtures for tests of other review subsystems.

Each invocation has its own change and ledger. Fresh-panel tests receive an
exhausted predecessor. The real reservation still runs; none of the panel,
transport, verdict or journal assertions is bypassed. Cross-invocation and Git
identity behavior is exercised separately by test_review_rounds.
"""

import shutil
import tempfile
import weakref
from pathlib import Path
from unittest.mock import patch


def isolated_reservation(module, original, args):
    root = tempfile.mkdtemp(prefix="review-round-fixture-")
    weakref.finalize(args, shutil.rmtree, root)
    with patch.object(module, "round_ledger_root", return_value=Path(root).resolve()), \
            patch.object(module, "change_identity", return_value="a" * 64):
        if args.fresh_cycle_from:
            for number in range(1, module.MAX_ROUNDS + 1):
                module.reserve_round("a" * 64, number, args.fresh_cycle_from)
        for number in range(1, args.round):
            module.reserve_round("a" * 64, number, args.selected_profile,
                                 args.fresh_cycle_from)
        return original(args)
