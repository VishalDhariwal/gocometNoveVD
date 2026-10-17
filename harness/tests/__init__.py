import os

# The case reviewer (nova/review) calls Claude during `report`. Tests that don't test it keep it off, so a developer
# with an API key in the shell never spends money running the suite; tests/test_review.py turns it on explicitly.
os.environ.setdefault("NOVA_REVIEW", "off")
# `report` first reads any document ingest didn't get to. Test worlds are written straight into the store, so their
# documents look unread; tests that test the catch-up turn it on explicitly.
os.environ.setdefault("NOVA_REPORT_READS", "off")
