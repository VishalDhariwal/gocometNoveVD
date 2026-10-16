import os

# The case reviewer (nova/review) calls Claude during `report`. Tests that don't test it keep it off, so a developer
# with an API key in the shell never spends money running the suite; tests/test_review.py turns it on explicitly.
os.environ.setdefault("NOVA_REVIEW", "off")
