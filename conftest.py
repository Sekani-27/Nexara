"""
Root conftest.py — makes `trader_copilot` importable from every test file
without per-file sys.path manipulation.

pytest automatically adds any directory containing a conftest.py to sys.path.
This file sits at the project root (same level as the trader_copilot/ package),
so `from trader_copilot.X import Y` resolves correctly in all four test files.
"""
