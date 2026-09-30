"""Bank position alerts (spec 7e): daily digest, critical / warning alerts and the entry-time low balance guard.

Read only: nothing here posts accounting. `positions` evaluates the per-bank projections of
treasury.pdc.bank_positions against the thresholds of Treasury Company Settings.
"""
