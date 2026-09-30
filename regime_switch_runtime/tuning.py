"""Bounded, expiring parameter proposals from an advisory agent.

The agent never places orders and never touches dollar caps, stops, opening
limits or the halt latch. It may only choose values from a pre-declared
envelope, at a bounded cadence. A proposal expires on its own and the fixed
rule resumes, so a silent or broken agent degrades to the tested policy.

Safety pauses issued by the deterministic operator (equity kill switch, its
cooldown, a crash in progress, missing or warming-up data) cannot be
overridden. Classifier pauses (trend, whipsaw, funding, ATR) can, because the
paired ablations showed those pauses only forfeit fee income; overriding them
is exactly the "always on with different intensity" hypothesis under test.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math

SAFETY_PAUSE_PREFIXES = ('kill switch', 'cooldown', 'crash', 'missing')


@dataclass(frozen=True)
class TuningEnvelope:
    hedge_ratios: tuple = (0.5, 0.8, 1.0)
    capital_fractions: tuple = (0.0, 0.4, 0.7, 1.0)
    range_widths: tuple = (0.01, 0.02)
    min_interval_seconds: int = 4 * 3600
    ttl_seconds: int = 6 * 3600
    max_rationale_chars: int = 600

    def describe(self):
        return {'hedge_ratio': list(self.hedge_ratios), 'capital_fraction': list(self.capital_fractions),
                'range_width_pct': list(self.range_widths),
                'min_interval_hours': self.min_interval_seconds / 3600, 'ttl_hours': self.ttl_seconds / 3600,
                'frozen': 'dollar caps, stops, opening limit, halt latch, safety pauses'}


# One Meteora position account holds at most 69 bins; at 4 bps per bin the
# widest symmetric range that fits is about +/-1.3%.
# Twelve hours between parameter changes, and an eighteen-hour expiry so one
# accepted proposal still covers the gap until the next review.
COMPETITION_ENVELOPE = TuningEnvelope(range_widths=(.007, .01, .013),
                                      min_interval_seconds=12 * 3600, ttl_seconds=18 * 3600)


@dataclass
class TuningProposal:
    hedge_ratio: float
    capital_fraction: float
    range_width_pct: float
    rationale: str
    at: int
    expires_at: int
    source: str = 'agent'

    def to_dict(self):
        return asdict(self)


def _pick(value, allowed, name):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{name} must be a number from {list(allowed)}')
    if not math.isfinite(v):
        raise ValueError(f'{name} must be finite')
    for a in allowed:
        if abs(v - a) <= 1e-9:
            return float(a)
    raise ValueError(f'{name}={v} is outside the envelope {list(allowed)}')


def validate_proposal(raw: dict, envelope: TuningEnvelope, now: int,
                      last_applied_at: int | None = None) -> TuningProposal:
    """Coerce an untrusted proposal into the envelope or raise ValueError.

    ``expires_at`` is computed here, never trusted from the caller. A proposal
    inside the minimum interval of the previously applied one is rejected so
    an agent cannot churn the position faster than the tested cadence.
    """
    if not isinstance(raw, dict):
        raise ValueError('Proposal must be an object')
    if type(now) is not int:
        raise ValueError('Timestamp must be an integer')
    rationale = str(raw.get('rationale') or '').strip()
    if not rationale:
        raise ValueError('A rationale is required; it is audited')
    proposal = TuningProposal(
        hedge_ratio=_pick(raw.get('hedge_ratio'), envelope.hedge_ratios, 'hedge_ratio'),
        capital_fraction=_pick(raw.get('capital_fraction'), envelope.capital_fractions, 'capital_fraction'),
        range_width_pct=_pick(raw.get('range_width_pct'), envelope.range_widths, 'range_width_pct'),
        rationale=rationale[:envelope.max_rationale_chars],
        at=now, expires_at=now + envelope.ttl_seconds,
        source=str(raw.get('source') or 'agent')[:40])
    if last_applied_at is not None and now - last_applied_at < envelope.min_interval_seconds:
        raise ValueError('Proposal rejected: inside the minimum change interval')
    return proposal


def is_safety_pause(decision: dict) -> bool:
    return (not decision.get('lp_on', False)) and str(decision.get('reason', '')).startswith(SAFETY_PAUSE_PREFIXES)


def apply_tuning(decision: dict, proposal: TuningProposal | dict | None, now: int) -> dict:
    """Return the decision with the proposal applied inside the envelope rules.

    - No proposal, or an expired one: the fixed rule stands.
    - Safety pause: the fixed rule stands; the proposal is recorded as ignored.
    - Otherwise the agent's hedge ratio, capital fraction and width replace the
      rule's; a zero fraction is an agent pause, a positive fraction on a
      classifier pause is an agent override.
    """
    out = dict(decision)
    if proposal is None:
        out['tuning'] = None
        return out
    p = proposal if isinstance(proposal, dict) else proposal.to_dict()
    meta = {'at': p['at'], 'expires_at': p['expires_at'], 'source': p.get('source', 'agent')}
    if now > p['expires_at']:
        out['tuning'] = {**meta, 'applied': False, 'status': 'expired'}
        return out
    if is_safety_pause(out):
        out['tuning'] = {**meta, 'applied': False, 'status': 'safety pause not overridable'}
        return out
    if p['capital_fraction'] <= 0:
        out.update(lp_on=False, hedge_ratio=0.0, capital_fraction=0.0, profile='agent_pause',
                   reason='agent: ' + p['rationale'][:120])
    else:
        overriding = not out.get('lp_on', False)
        out.update(lp_on=True, hedge_ratio=p['hedge_ratio'], capital_fraction=p['capital_fraction'],
                   range_width_pct=p['range_width_pct'],
                   profile='agent_override' if overriding else 'agent_tuned',
                   reason=('agent override of classifier pause: ' if overriding else 'agent: ') + p['rationale'][:120])
    out['tuning'] = {**meta, 'applied': True, 'status': 'applied'}
    return out


# ---------------------------------------------------------------- live plumbing

def tuning_path(state_path):
    from pathlib import Path
    p = Path(state_path)
    return p.with_name(p.name + '.tuning.json')


def read_proposal(path, envelope: TuningEnvelope):
    """Load and re-validate a persisted proposal; None if absent or invalid.

    The engine never trusts the file: values are re-checked against the
    envelope, and the expiry is recomputed from the recorded ``at``.
    """
    import json
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
        proposal = TuningProposal(
            hedge_ratio=_pick(raw['hedge_ratio'], envelope.hedge_ratios, 'hedge_ratio'),
            capital_fraction=_pick(raw['capital_fraction'], envelope.capital_fractions, 'capital_fraction'),
            range_width_pct=_pick(raw['range_width_pct'], envelope.range_widths, 'range_width_pct'),
            rationale=str(raw.get('rationale', ''))[:envelope.max_rationale_chars],
            at=int(raw['at']), expires_at=int(raw['at']) + envelope.ttl_seconds,
            source=str(raw.get('source', 'agent'))[:40])
        if proposal.at < 0:
            raise ValueError('negative timestamp')
        return proposal
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def write_proposal(path, raw: dict, envelope: TuningEnvelope, now: int) -> TuningProposal:
    """Validate against the envelope and the previously persisted proposal, then persist atomically."""
    from .state import save
    previous = read_proposal(path, envelope)
    last = previous.at if previous else None
    if previous and (previous.hedge_ratio, previous.capital_fraction, previous.range_width_pct) == (
            _pick(raw.get('hedge_ratio'), envelope.hedge_ratios, 'hedge_ratio'),
            _pick(raw.get('capital_fraction'), envelope.capital_fractions, 'capital_fraction'),
            _pick(raw.get('range_width_pct'), envelope.range_widths, 'range_width_pct')):
        last = None  # re-affirming the same values only refreshes the expiry; it moves nothing
    proposal = validate_proposal(raw, envelope, now, last)
    save(path, proposal.to_dict())
    return proposal


HISTORY_BARS = 72


def history_record(obs, cfg, state, decision: dict) -> dict:
    """Compact hourly snapshot the engine appends for later diagnosis."""
    lp = obs.lp_positions[0] if obs.lp_positions else None
    fees = (float(lp.get('base_fee_amount') or 0) * obs.price + float(lp.get('quote_fee_amount') or 0)) if lp else 0.0
    in_range = bool(lp and float(lp['lower_price']) <= obs.price <= float(lp['upper_price']))
    return {'t': obs.timestamp, 'feature_t': obs.feature_timestamp, 'equity': round(obs.equity(cfg), 6),
            'lp_value': round(obs.lp_value(), 6), 'unclaimed_fees_usd': round(fees, 6),
            'hedge_equity': round(obs.hedge_equity, 6), 'hedge_units': obs.hedge_units,
            'wallet_usdc': round(obs.wallet_usdc, 6), 'wallet_sol': obs.wallet_sol,
            'price': obs.price, 'hedge_price': obs.hedge_price, 'lp_on': lp is not None, 'in_range': in_range,
            'openings': state.get('openings', 0), 'profile': decision.get('profile'), 'reason': decision.get('reason'),
            'regime': (obs.features or {}).get('regime'),
            'halted': bool(state['capital']['halted']), 'halt_reason': state['capital']['reason'] or None,
            'tuning': (decision.get('tuning') or {}).get('status')}


def diagnosis_packet(state: dict, cfg, now: int, envelope: TuningEnvelope, lookback: int = 24,
                     proposal: TuningProposal | None = None) -> dict:
    """What the advisory agent is allowed to see: realised history, the rule's view, the envelope."""
    hist = list(state.get('history') or [])[-lookback:]
    first, last = (hist[0], hist[-1]) if hist else (None, None)
    attribution = {}
    if hist:
        attribution = {
            'net_equity_change': round(last['equity'] - first['equity'], 4),
            'unclaimed_lp_fees_now_usd': last['unclaimed_fees_usd'],
            'hedge_equity_change': round(last['hedge_equity'] - first['hedge_equity'], 4),
            'lp_openings': int(last['openings'] - first['openings']),
            'hours_with_lp': int(sum(1 for h in hist if h['lp_on'])),
            'hours_in_range': int(sum(1 for h in hist if h['in_range'])),
            'price_change_pct': round((last['price'] / first['price'] - 1) * 100, 2),
            'hours_covered': len(hist),
            'note': 'wallet-level equity already nets swap, rent and gas costs; fee income is only visible as unclaimed fees until a close',
        }
    rule = state.get('rule_decision') or state.get('decision') or {}
    lp = (state.get('last_observation') or {})
    return {
        'task': 'Diagnose the last day, then either hold or propose parameters inside the envelope.',
        'now': now, 'equity_usd': lp.get('equity'),
        'fixed_rule_says': {k: rule.get(k) for k in ('lp_on', 'profile', 'reason', 'hedge_ratio', 'capital_fraction', 'range_width_pct')},
        'features': {'regime': (last or {}).get('regime'), 'feature_timestamp': state.get('last_feature_timestamp')},
        'last_24h_attribution_usd': attribution,
        'current_position': {'lp_value_usd': lp.get('lp_value'), 'hedge_units': lp.get('hedge_units'),
                             'openings_used': state.get('openings'), 'openings_cap': cfg.max_openings,
                             'halted': state['capital']['halted'], 'halt_reason': state['capital']['reason'] or None,
                             'lp_fraction': state.get('lp_fraction'), 'lp_width': state.get('lp_width')},
        'active_proposal': proposal.to_dict() if proposal else None,
        'last_applied_tuning': state.get('tuning'),
        'envelope': envelope.describe(),
        'rules': ('capital_fraction 0 pauses the LP; a positive fraction during a classifier pause keeps the LP on; '
                  'safety pauses and the capital halt cannot be overridden; changing capital_fraction or range_width_pct '
                  'closes and reopens the position and consumes one of the limited openings; hedge_ratio changes only re-hedge, '
                  'and a re-hedge below the venue $10 order minimum is not traded.'),
        'respond_with': {'diagnosis': 'one or two sentences', 'hedge_ratio': 'number', 'capital_fraction': 'number',
                         'range_width_pct': 'number', 'rationale': 'short', 'confidence': '0-1'},
    }
