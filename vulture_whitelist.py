"""Names vulture must not report as unused.

A protocol method's parameters are part of the interface, not dead code: they are
unused inside the protocol body by construction, and renaming them would change
the keyword-call surface that implementers and callers rely on. Vulture cannot
know that, so the interface is declared here explicitly rather than silenced with
a broad ignore.

The format is vulture's own (`--make-whitelist`): bare names, which is how it
records a name as used. A call with keyword arguments does NOT register them —
that was the first attempt, and the finding survived it.

Usage: vulture src vulture_whitelist.py --min-confidence 80
"""

system_prompt  # unused variable (src/cartera/ports/store.py:43)
user_prompt  # unused variable (src/cartera/ports/store.py:43)
