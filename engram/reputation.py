"""Dynamic trust: each device keeps its own Beta reputation for every peer.

    effective_trust = min(operator_trust,
                          (PRIOR * operator_trust + good) / (PRIOR + good + BAD_WEIGHT * bad))

  operator_trust  the hub's static score; a cap, never exceeded, so a device
                  cannot earn its way above what the operator assigned
  good            memories from that peer this device promoted or corroborated
  bad             memories *provably signed by that peer* (valid signature under
                  the pinned key) that this device quarantined for their content

Only provably-signed misbehavior counts against a device. An unsigned or forged
memory that merely claims to come from the kiosk tells us nothing about the
kiosk, so an attacker cannot frame an honest device into losing trust.

The asymmetry is deliberate: one poisoned memory costs as much as BAD_WEIGHT
good ones, so a stolen trusted key loses its power after a few attempts.
Every device computes this from its own observations only; nothing about
reputation is synced or trusted from the hub.
"""

PRIOR = 2.0
BAD_WEIGHT = 4.0

# Quarantine reasons that judge the memory's *content*. Provenance/trust failures are not
# evidence about the signer: either the signer is unproven, or we would double-count.
CONTENT_REASONS = ("contradicts_corroborated_memory", "semantic_conflict_with_corroborated_memory",
                   "nli_contradiction_with_corroborated_memory")


def effective_trust(operator_trust: float, good: float = 0, bad: float = 0) -> float:
    beta_mean = (PRIOR * operator_trust + good) / (PRIOR + good + BAD_WEIGHT * bad)
    return round(min(operator_trust, beta_mean), 4)


def counts_against_signer(reasons: list[str]) -> bool:
    return any(r in CONTENT_REASONS for r in reasons)


def describe(operator_trust: float, good: float, bad: float) -> dict:
    eff = effective_trust(operator_trust, good, bad)
    return {
        "operator_trust": operator_trust,
        "good": good,
        "bad": bad,
        "effective_trust": eff,
        "formula": f"min({operator_trust:.2f}, ({PRIOR:g}x{operator_trust:.2f} + {good:g}) / "
                   f"({PRIOR:g} + {good:g} + {BAD_WEIGHT:g}x{bad:g}))",
    }
