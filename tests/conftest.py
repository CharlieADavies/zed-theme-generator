"""Hypothesis profiles: fast dev runs by default, thorough via HYPOTHESIS_PROFILE."""

import os

from hypothesis import HealthCheck, Phase, settings

# A full theme build costs ~80ms, so example counts are budgeted rather than
# left on the Hypothesis defaults.
settings.register_profile(
    "dev",
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
# Debug: surface raw counterexamples without paying for the shrink loop.
settings.register_profile(
    "noshrink",
    max_examples=50,
    deadline=None,
    phases=(Phase.explicit, Phase.reuse, Phase.generate),
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "thorough",
    max_examples=250,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
