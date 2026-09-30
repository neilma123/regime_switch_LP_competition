from dataclasses import asdict, dataclass
import hashlib
import json
import math

from .capital_guard import CapitalLimits

SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
POOL = "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"

# Capital partitions that have been reviewed end to end. Any other partition
# is rejected rather than silently trusted, so an LLM routine call cannot
# type a new dollar budget into production.
#   (initial_equity, user_max_loss, max_lp, hedge_collateral, operating_reserve)
APPROVED_PROFILES = {
    'competition_800': (800, 100, 40, 30, 10),
    'rehearsal_50': (50, 50, 28, 12, 10),
}


@dataclass(frozen=True)
class Settings:
    live: bool = False
    account_name: str = ""
    solana_wallet: str = ""
    hyperliquid_address: str = ""
    controller_id: str = "regime_switch_lp_competition"
    race_start: int = 0
    race_end: int = 0
    deployment_verified: bool = False
    # 'external': the protected reserve is held outside the working wallets and
    # is added to equity as an attested constant (the original design).
    # 'wallet_usdc': the whole account sits in the working wallets as idle
    # USDC; equity is observed inventory only, and the reserve is protected by
    # the same LP/hedge exposure caps rather than by physical separation.
    reserve_mode: str = "external"
    initial_equity_usd: float = 800
    user_max_loss_usd: float = 100
    protected_reserve_usd: float = 720
    max_lp_usd: float = 40
    hedge_collateral_usd: float = 30
    operating_reserve_usd: float = 10
    equity_tolerance_usd: float = 2
    position_loss_usd: float = 2
    daily_loss_usd: float = 8
    total_loss_usd: float = 24
    max_net_delta_usd: float = 20
    max_openings: int = 2
    exit_buffer_seconds: int = 1800
    max_pending_seconds: int = 120
    max_lp_pending_seconds: int = 600
    max_failures: int = 3
    # Features are one completed hourly bar old by construction (3600–7200 s).
    # Beyond the grace age no new position is opened; beyond the hard age the
    # run latches (a data outage, not an hour-boundary artefact).
    feature_grace_seconds: int = 8100
    max_feature_age_seconds: int = 14400
    max_position_age_seconds: int = 86400
    min_gas_sol: float = .02
    entry_rent_and_gas_sol: float = .08
    # Priority fees / wrapped-SOL account rent absorbed by a swap (~$0.4 max).
    swap_tolerance_sol: float = .003
    # Advisory agent proposals (see tuning.py). Off by default: the agent can
    # only move hedge ratio, LP fraction and width inside the envelope, never
    # dollar caps, stops, openings or the halt latch. Proposals live in
    # <state file>.tuning.json and expire on their own.
    tuning_enabled: bool = False

    def partition(self):
        return (self.initial_equity_usd, self.user_max_loss_usd, self.max_lp_usd,
                self.hedge_collateral_usd, self.operating_reserve_usd)

    def limits(self):
        return CapitalLimits(initial_equity=self.initial_equity_usd, user_max_loss=self.user_max_loss_usd,
            max_lp_usd=self.max_lp_usd, hedge_collateral_usd=self.hedge_collateral_usd,
            operating_reserve_usd=self.operating_reserve_usd, daily_loss_usd=self.daily_loss_usd,
            total_loss_usd=self.total_loss_usd, drawdown_usd=self.total_loss_usd,
            position_loss_usd=self.position_loss_usd, max_net_delta_usd=self.max_net_delta_usd,
            max_failures=self.max_failures)

    def validate(self):
        if type(self.live) is not bool or type(self.deployment_verified) is not bool or type(self.tuning_enabled) is not bool:
            raise ValueError("Live/deployment/tuning flags must be explicit booleans")
        if self.reserve_mode not in ('external','wallet_usdc'):
            raise ValueError("reserve_mode must be 'external' or 'wallet_usdc'")
        money=(self.initial_equity_usd,self.user_max_loss_usd,self.max_lp_usd,
               self.hedge_collateral_usd,self.operating_reserve_usd,self.equity_tolerance_usd,
               self.position_loss_usd,self.daily_loss_usd,self.total_loss_usd,self.max_net_delta_usd)
        if not all(isinstance(v,(int,float)) and math.isfinite(v) and v>0 for v in money):
            raise ValueError('Capital amounts must be positive and finite')
        if not (isinstance(self.protected_reserve_usd,(int,float)) and math.isfinite(self.protected_reserve_usd)
                and self.protected_reserve_usd>=0):
            raise ValueError('Protected reserve must be a finite nonnegative amount')
        # Dollar risk values are frozen to reviewed partitions, not editable by an LLM routine call.
        if self.partition() not in APPROVED_PROFILES.values():
            raise ValueError(f"Capital partition {self.partition()} is not a reviewed profile: {APPROVED_PROFILES}")
        working=self.max_lp_usd+self.hedge_collateral_usd+self.operating_reserve_usd
        if self.reserve_mode=='external':
            if abs(self.protected_reserve_usd+working-self.initial_equity_usd)>1e-9:
                raise ValueError('External reserve plus working capital must equal the initial equity')
        elif self.protected_reserve_usd!=self.initial_equity_usd-working:
            raise ValueError('wallet_usdc mode: protected_reserve_usd must equal initial equity minus working capital')
        if (self.position_loss_usd,self.daily_loss_usd,self.total_loss_usd,self.max_net_delta_usd)!=(2,8,24,20):
            raise ValueError('Dollar stops and delta cap are frozen in this profile')
        if not 0<self.equity_tolerance_usd<=max(2,.05*self.initial_equity_usd):
            raise ValueError('Equity reconciliation tolerance must stay tight')
        self.limits()  # enforces the guard invariants for this partition
        if self.max_openings != 2:
            raise ValueError("This profile permits at most two LP openings per race")
        for key in ('race_start','race_end','max_openings','exit_buffer_seconds','max_pending_seconds',
                    'max_lp_pending_seconds','max_failures','feature_grace_seconds','max_feature_age_seconds',
                    'max_position_age_seconds'):
            if type(getattr(self,key)) is not int or getattr(self,key)<0:
                raise ValueError(f'{key} must be a nonnegative integer')
        if (self.max_pending_seconds!=120 or self.max_lp_pending_seconds!=600 or self.max_failures!=3
                or self.min_gas_sol!=.02 or self.entry_rent_and_gas_sol!=.08 or self.swap_tolerance_sol!=.003):
            raise ValueError('Pending timeout, failure and gas safeguards are frozen in this profile')
        if not 7200<self.feature_grace_seconds<=self.max_feature_age_seconds<=6*3600:
            raise ValueError('Feature staleness thresholds must exceed one bar of lag and stay within six hours')
        if not 3600<=self.max_position_age_seconds<=86400:
            raise ValueError('Maximum position age must be between one hour and one day')
        if self.controller_id!='regime_switch_lp_competition': raise ValueError('Unexpected executor namespace')
        if self.live and (not self.deployment_verified or not self.account_name
                or not self.solana_wallet or not self.hyperliquid_address
                or self.race_start <= 0 or self.race_end-self.race_start != 48*3600):
            raise ValueError("Live mode requires verified Botcamp account bindings and an exact 48-hour interval")
        if self.exit_buffer_seconds < 600 or self.exit_buffer_seconds >= 3600:
            raise ValueError("End-of-race exit buffer must be 10–60 minutes")

    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self),sort_keys=True).encode()).hexdigest()
