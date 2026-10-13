"""Readers: turn a stored document into facts.

The split of work is the core rule of the harness: Claude copies values exactly as printed and says where they are;
code converts them (values.py) and checks them against the page (verify.py). Nothing a reader stores is a number the
model computed. Facts that can't be grounded are stored as 'unverified', and later stages treat them with suspicion.
"""
