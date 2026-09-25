"""Deterministic one-action-at-a-time coordinator. No LLM chooses order sizes.

The broker supplies independently observed wallet, LP and perpetual inventory.
Intents are persisted before submission. Ambiguous outcomes are never blindly
resubmitted. This module contains no credential material or private-key handling.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import uuid

from .settings import Settings
from .state import exclusive, save
from .operator import OperatorState, decide
from .profile import regime_configs
from .capital_guard import CapitalLimits, CapitalState, check_capital


@dataclass
class Observation:
    timestamp: int
    price: float
    hedge_price: float
    wallet_usdc: float
    wallet_sol: float
    native_sol: float
    hedge_equity: float
    hedge_units: float
    lp_positions: list
    executors: list
    rent_sol: float
    feature_timestamp: int
    features: dict
    data_age_seconds: float = 0
    foreign_exposure: bool = False
    hedge_leverage: float = 1

    def lp_value(self):
        return sum(float(p['base_token_amount'])*self.price+float(p['quote_token_amount'])
                   +float(p.get('base_fee_amount') or 0)*self.price+float(p.get('quote_fee_amount') or 0)
                   for p in self.lp_positions)

    def equity(self, cfg):
        return cfg.protected_reserve_usd+self.wallet_usdc+self.wallet_sol*self.price+self.lp_value()+self.rent_sol*self.price+self.hedge_equity


def _fresh_state(cfg, obs):
    if obs.lp_positions or obs.executors or abs(obs.hedge_units)>1e-9:
        raise ValueError("Missing state with existing exposure: refusing ownership adoption")
    if obs.wallet_usdc+obs.wallet_sol*obs.price > 50.5 or obs.hedge_equity > 30.5:
        raise ValueError("Working wallets exceed the agreed reserve separation")
    equity=obs.equity(cfg)
    if not 798 <= equity <= 802:
        raise ValueError("Initial observed account value does not reconcile to $800")
    return {'version':1,'fingerprint':cfg.fingerprint(),'gas_baseline_sol':obs.wallet_sol,
        'capital':asdict(CapitalState()),'operator':asdict(OperatorState(peak_equity=800)),
        'last_feature_timestamp':-1,'decision':None,'pending':None,'lp_executor':None,
        'lp_address':None,'entry_equity':None,'opened_at':None,'openings':0,'failures':0,'audit':[]}


def _halt(state,reason):
    state['capital']['halted']=True
    if not state['capital']['reason']: state['capital']['reason']=reason


async def tick(cfg:Settings, broker, state_path:Path):
    cfg.validate()
    with exclusive(state_path):
        state=json.loads(state_path.read_text()) if state_path.exists() else None
        if state and (state.get('version')!=1 or state.get('fingerprint')!=cfg.fingerprint()):
            raise ValueError("State/config mismatch; never start a fresh risk epoch automatically")
        if state:
            c=state['capital']
            if (type(c['halted']) is not bool or (c['halted'] and not c['reason'])
                    or any(not math.isfinite(c[k]) or c[k]<=0 for k in
                           ('peak_equity','day_open_equity','last_equity'))
                    or type(state['openings']) is not int or not 0<=state['openings']<=cfg.max_openings
                    or not math.isfinite(state['gas_baseline_sol']) or state['gas_baseline_sol']<0):
                raise ValueError('Invalid persisted risk state; refusing automatic reset')
        try:
            obs=await broker.observe(cfg)
            for value in [obs.timestamp,obs.price,obs.hedge_price,obs.wallet_usdc,obs.wallet_sol,
                          obs.native_sol,obs.hedge_equity,obs.hedge_units,obs.rent_sol,obs.data_age_seconds]:
                if not math.isfinite(value): raise ValueError("Non-finite account/market observation")
            if min(obs.price,obs.hedge_price)<=0: raise ValueError("Invalid market price")
            if min(obs.wallet_usdc,obs.wallet_sol,obs.native_sol,obs.rent_sol)<0:
                raise ValueError('Invalid negative wallet inventory')
            for p in obs.lp_positions:
                for key in ('base_token_amount','quote_token_amount','lower_price','upper_price',
                            'base_fee_amount','quote_fee_amount'):
                    if not math.isfinite(float(p[key])) or float(p[key])<0:
                        raise ValueError('Invalid LP inventory')
                if not 0<float(p['lower_price'])<float(p['upper_price']):
                    raise ValueError('Invalid LP bounds')
        except Exception as exc:
            if state:
                state['failures']+=1
                _halt(state,'observation/reconciliation unavailable')
                save(state_path,state)
                # Known LP stops are idempotent. Never guess a new hedge order
                # from stale inventory while its fill status is unknown.
                if cfg.live and state.get('lp_executor'):
                    try: await broker.stop(state['lp_executor'])
                    except Exception: pass
            return {'status':'HALTED_UNVERIFIED','reason':type(exc).__name__}

        if state is None:
            state=_fresh_state(cfg,obs)
            save(state_path,state)
        capital=CapitalState(**state['capital'])
        reason=check_capital(state=capital,limits=CapitalLimits(),timestamp=obs.timestamp,
            equity=obs.equity(cfg),data_age_seconds=obs.data_age_seconds,
            basis_bps=(obs.price/obs.hedge_price-1)*10000,
            reconciliation_ok=not obs.foreign_exposure,consecutive_failures=state['failures'])
        state['capital']=asdict(capital)
        state['last_observation']={'timestamp':obs.timestamp,'equity':obs.equity(cfg),
            'lp_value':obs.lp_value(),'hedge_units':obs.hedge_units,'hedge_equity':obs.hedge_equity,
            'wallet_usdc':obs.wallet_usdc,'wallet_sol':obs.wallet_sol,'cex_quote_pnl':0.0}
        if obs.hedge_leverage>1: _halt(state,'hedge leverage exceeds 1x')
        if abs(obs.hedge_units)*obs.hedge_price>30.5: _halt(state,'hedge notional limit')
        if obs.native_sol<cfg.min_gas_sol: _halt(state,'insufficient native SOL for exits')
        if cfg.race_end and obs.timestamp >= cfg.race_end-cfg.exit_buffer_seconds:
            _halt(state,'scheduled end-of-race unwind')
        if state['entry_equity'] is not None and state['entry_equity']-obs.equity(cfg)>=2:
            _halt(state,'position dollar loss stop')
        if state['opened_at'] and obs.timestamp-state['opened_at']>=24*3600:
            _halt(state,'maximum position age reached')

        if obs.feature_timestamp!=state['last_feature_timestamp']:
            if obs.feature_timestamp<state['last_feature_timestamp']:
                _halt(state,'out-of-order feature data')
            operator=OperatorState(**state['operator'])
            _,op_cfg=regime_configs()
            decision=decide(obs.features,equity=obs.equity(cfg),state=operator,config=op_cfg)
            state.update(operator=asdict(operator),decision=asdict(decision),
                         last_feature_timestamp=obs.feature_timestamp)
        decision=state['decision'] or {'lp_on':False,'capital_fraction':0,'hedge_ratio':0}
        if obs.timestamp-obs.feature_timestamp>7200 or obs.feature_timestamp>obs.timestamp:
            _halt(state,'stale or future feature data')
        active=[e for e in obs.executors if str(e.get('status','')).upper() not in {'TERMINATED','COMPLETED'}]
        by_id={str(e.get('id') or e.get('executor_id')):e for e in obs.executors}
        pending=state['pending']
        if pending:
            found=by_id.get(pending['id'])
            kind=pending['kind']
            confirmed=False
            if found:
                if kind=='lp' and len(obs.lp_positions)==1:
                    state.update(lp_executor=pending['id'],lp_address=obs.lp_positions[0]['position_address'],
                        opened_at=pending['at'],entry_equity=pending['entry_equity'])
                    confirmed=True
                elif kind in {'acquire','sweep','hedge'} and str(found.get('status','')).upper() in {'TERMINATED','COMPLETED'}:
                    # Status alone is not proof of a fill. Reconcile actual inventory.
                    actual=obs.hedge_units if kind=='hedge' else obs.wallet_sol
                    tolerance=1e-8 if kind=='hedge' else .001
                    confirmed=abs(actual-pending['expected'])<=tolerance
                    if not confirmed:
                        _halt(state,'executor ended without inventory reconciliation')
                        # A terminal executor cannot fill again. Clear its intent
                        # and unwind the independently observed partial inventory.
                        # An unknown/non-terminal executor must remain pending.
                        state['pending']=None
                        pending=None
                elif kind=='lp' and not obs.lp_positions and str(found.get('status','')).upper() in {'TERMINATED','COMPLETED'}:
                    _halt(state,'LP executor ended without a verified position')
                    state['pending']=None
                    pending=None
            if confirmed:
                state['pending']=None
                pending=None
            elif pending:
                if obs.timestamp-pending['at']>cfg.max_pending_seconds:
                    _halt(state,'ambiguous or timed-out transaction; no blind retry')
                if state['capital']['halted']:
                    save(state_path,state)
                    if cfg.live:
                        if state.get('lp_executor'):
                            await _close_owned_lp(cfg,broker,state_path,state,obs,by_id)
                        if found and pending['id']!=state.get('lp_executor'): await broker.stop(pending['id'])
                save(state_path,state)
                return {'status':'WAITING_RECONCILIATION','halted':state['capital']['halted'],'intent':pending['id']}

        if state['lp_address'] and not obs.lp_positions:
            closed=by_id.get(state['lp_executor'])
            if ((not closed or str(closed.get('status','')).upper() not in {'TERMINATED','COMPLETED'})
                    and state.get('retired_lp_executor')!=state['lp_executor']):
                # LP executors may still have a close-out swap in flight after
                # on-chain withdrawal. Do not race it with our own wallet sweep.
                save(state_path,state)
                if cfg.live:
                    response=await broker.stop(state['lp_executor'])
                    if isinstance(response,dict) and response.get('status')=='already_terminated':
                        state['retired_lp_executor']=state['lp_executor']
                        save(state_path,state)
                return {'status':'WAITING_LP_CLOSE_OUT','executor':state['lp_executor']}
            state.update(lp_executor=None,lp_address=None,entry_equity=None,opened_at=None,
                         retired_lp_executor=None,lp_close_attempts=0,lp_close_last_at=0)
        if obs.lp_positions and (len(obs.lp_positions)!=1 or obs.lp_positions[0]['position_address']!=state['lp_address']):
            _halt(state,'unrecognized on-chain LP position')
            save(state_path,state)
            return {'status':'HALTED_UNRECOGNIZED_POSITION'}
        owner=by_id.get(state['lp_executor'],{})
        if obs.lp_positions and str(owner.get('status','')).upper() in {'TERMINATED','COMPLETED'}:
            _halt(state,'LP executor terminated with a live on-chain position')
        new_entry_allowed=(decision['lp_on'] and not state['capital']['halted']
            and (not cfg.race_start or obs.timestamp>=cfg.race_start)
            and (not cfg.race_end or obs.timestamp<cfg.race_end-cfg.exit_buffer_seconds))
        want_close=not new_entry_allowed
        if obs.lp_positions:
            p=obs.lp_positions[0]
            if state.get('lp_fraction') != decision['capital_fraction']: want_close=True
            center=(float(p['lower_price'])+float(p['upper_price']))/2
            if abs(obs.price/center-1)*10000>=150: want_close=True
            if want_close:
                save(state_path,state)
                return await _close_owned_lp(cfg,broker,state_path,state,obs,by_id)
            lp_base=float(p['base_token_amount'])
            target=-math.floor(min(lp_base*decision['hedge_ratio'],30/obs.hedge_price)/.01)*.01
            net_delta=(obs.wallet_sol+lp_base+target)*obs.hedge_price
            if abs(net_delta)>20:
                _halt(state,'residual delta cannot be hedged inside the notional cap')
                save(state_path,state)
                return await _close_owned_lp(cfg,broker,state_path,state,obs,by_id)
        else:
            target=0.0

        # Flatten the hedge only after LP withdrawal is observed, so a failed
        # withdrawal cannot leave the former LP suddenly unhedged.
        change=target-obs.hedge_units
        if obs.lp_positions and abs(change)*obs.hedge_price<10:
            actual_delta=(obs.wallet_sol+lp_base+obs.hedge_units)*obs.hedge_price
            if abs(actual_delta)>20:
                _halt(state,'actual residual delta exceeds limit below hedge minimum')
                save(state_path,state)
                return await _close_owned_lp(cfg,broker,state_path,state,obs,by_id)
        if abs(change)*obs.hedge_price>=10 or (target==0 and abs(obs.hedge_units)>1e-9):
            if state['capital']['halted'] and obs.lp_positions:
                save(state_path,state)
                return await _close_owned_lp(cfg,broker,state_path,state,obs,by_id)
            return await _submit(cfg,broker,state_path,state,obs,'hedge',
                await broker.hedge_payload(cfg,obs,change),expected=target)

        surplus=max(0,obs.wallet_sol-state['gas_baseline_sol'])
        if not obs.lp_positions and state.get('prepared_lp') and new_entry_allowed:
            quote=state['prepared_lp']
            if (not quote['lower_price']<obs.price<quote['upper_price']
                    or quote['base_amount']*obs.price+quote['quote_amount']>quote['budget']
                    or quote['base_amount']*obs.price+quote['quote_amount']>cfg.max_lp_usd*decision['capital_fraction']
                    or quote['base_amount']>surplus+.001
                    or quote['quote_amount']>obs.wallet_usdc):
                _halt(state,'entry quote/inventory changed during acquisition')
                new_entry_allowed=False
            else:
                state['prepared_lp']=None
                state['lp_fraction']=decision['capital_fraction']
                return await _submit(cfg,broker,state_path,state,obs,'lp',broker.lp_payload(quote),expected=0)
        if not obs.lp_positions and surplus*obs.price>=1:
            # Sweep a failed acquisition or withdrawn residual before re-entry.
            return await _submit(cfg,broker,state_path,state,obs,'sweep',
                broker.spot_payload(-surplus),expected=state['gas_baseline_sol'])
        if not new_entry_allowed or obs.lp_positions or active or state['openings']>=cfg.max_openings:
            save(state_path,state)
            return {'status':'HALTED' if state['capital']['halted'] else 'MONITORING',
                    'reason':state['capital']['reason'],'decision':decision,'equity':obs.equity(cfg)}
        if obs.native_sol<cfg.entry_rent_and_gas_sol:
            save(state_path,state); return {'status':'NO_ENTRY','reason':'native SOL rent/fee reserve unavailable'}
        # Quote the actual symmetric split, acquire only its shortfall, then
        # reconcile the wallet before opening. Never submit both legs blindly.
        budget=cfg.max_lp_usd*decision['capital_fraction']
        quote=await broker.quote_lp(cfg,obs,budget)
        if quote['base_amount']*obs.price+quote['quote_amount']>budget+1e-6:
            raise ValueError('Position quote exceeds reservation')
        if quote['base_amount']>max(0,obs.wallet_sol-state['gas_baseline_sol'])+1e-9:
            # Persist intended LP quote across the acquisition so it is not
            # confused with a failed-entry residual on the next tick.
            state['prepared_lp']=quote
            buy=quote['base_amount']-max(0,obs.wallet_sol-state['gas_baseline_sol'])
            return await _submit(cfg,broker,state_path,state,obs,'acquire',broker.spot_payload(buy),expected=obs.wallet_sol+buy)
        state['lp_fraction']=decision['capital_fraction']
        return await _submit(cfg,broker,state_path,state,obs,'lp',broker.lp_payload(quote),expected=0)


async def _close_owned_lp(cfg,broker,path,state,obs,by_id):
    """Recover only a known LP whose backend executor has demonstrably retired.

    Closing one immutable on-chain position cannot create or oversell a second
    position. Repeat attempts are bounded and require a fresh owned-position
    observation; swaps/hedge orders never use this retry rule.
    """
    reason=state['capital']['reason'] or 'regime/recenter'
    if not cfg.live: return {'status':'DRY_RUN_CLOSE_LP','reason':reason}
    ident=state['lp_executor']
    retired=(str(by_id.get(ident,{}).get('status','')).upper() in {'TERMINATED','COMPLETED'}
             or state.get('retired_lp_executor')==ident)
    if not retired:
        response=await broker.stop(ident)
        retired=isinstance(response,dict) and response.get('status')=='already_terminated'
    if not retired: return {'status':'CLOSING_LP','reason':reason}
    _halt(state,'LP executor retired; recover owned on-chain liquidity')
    state['retired_lp_executor']=ident
    if (len(obs.lp_positions)!=1 or obs.lp_positions[0]['position_address']!=state['lp_address']):
        save(path,state)
        return {'status':'WAITING_OWNED_LP_RECONCILIATION'}
    attempts=state.get('lp_close_attempts',0)
    if attempts>=3 or (attempts and obs.timestamp-state['lp_close_last_at']<180):
        save(path,state)
        return {'status':'ORPHAN_WITHDRAWAL_UNVERIFIED','attempts':attempts}
    state['lp_close_attempts']=attempts+1
    state['lp_close_last_at']=obs.timestamp
    save(path,state)
    try:
        response=await broker.close_position(cfg,state['lp_address'])
        state['last_lp_close_signature']=response.get('transaction_hash')
        save(path,state)
        return {'status':'ORPHAN_WITHDRAWAL_SUBMITTED','attempts':attempts+1}
    except Exception:
        return {'status':'ORPHAN_WITHDRAWAL_UNVERIFIED','attempts':attempts+1}


async def _submit(cfg,broker,path,state,obs,kind,payload,expected):
    if not cfg.live:
        save(path,state)
        return {'status':'DRY_RUN_INTENT','kind':kind,'payload':payload,'equity':obs.equity(cfg)}
    ident='rslp-'+uuid.uuid4().hex
    pending={'id':ident,'kind':kind,'at':obs.timestamp,'expected':expected,'entry_equity':obs.equity(cfg)}
    if kind=='lp': state['openings']+=1
    state['pending']=pending
    state['audit'].append({'intent':ident,'kind':kind,'at':obs.timestamp})
    save(path,state)
    try:
        await broker.submit(cfg,ident,payload)
    except Exception:
        _halt(state,'submission outcome unknown; reconcile saved intent')
        save(path,state)
        return {'status':'UNKNOWN_SUBMISSION','intent':ident}
    return {'status':'SUBMITTED','intent':ident,'kind':kind}
