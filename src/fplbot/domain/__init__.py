"""Pure domain logic. No I/O, no AWS, no HTTP, no clock reads beyond what is
passed in.

This constraint is the single most valuable design decision in the codebase.
Every function here can be tested with plain Python objects and no mocking, and
the interesting bugs - a blank gameweek misclassified, an off-season crash, a
fuzzy match on the wrong Wilson - are all in here rather than in the I/O shell.
"""
