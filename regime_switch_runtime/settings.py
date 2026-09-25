from dataclasses import asdict, dataclass
import hashlib
import json
import math

SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
POOL = "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"


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
    protected_reserve_usd: float = 720
    max_lp_usd: float = 40
    hedge_collateral_usd: float = 30
    max_openings: int = 2
    exit_buffer_seconds: int = 1800
    max_pending_seconds: int = 120
    min_gas_sol: float = .02
    entry_rent_and_gas_sol: float = .08

    def validate(self):
        if type(self.live) is not bool or type(self.deployment_verified) is not bool:
            raise ValueError("Live/deployment flags must be explicit booleans")
        # Dollar risk values are frozen, not editable by an LLM routine call.
        if (self.max_lp_usd, self.hedge_collateral_usd, self.protected_reserve_usd) != (40,30,720):
            raise ValueError("This tested profile requires $40 LP, $30 hedge, $720 protected reserve")
        if self.max_openings != 2:
            raise ValueError("This profile permits at most two LP openings per race")
        for key in ('race_start','race_end','max_openings','exit_buffer_seconds','max_pending_seconds'):
            if type(getattr(self,key)) is not int or getattr(self,key)<0:
                raise ValueError(f'{key} must be a nonnegative integer')
        if self.max_pending_seconds!=120 or self.min_gas_sol!=.02 or self.entry_rent_and_gas_sol!=.08:
            raise ValueError('Pending timeout and gas safeguards are frozen in this profile')
        if not all(math.isfinite(v) for v in (self.protected_reserve_usd,self.max_lp_usd,self.hedge_collateral_usd)):
            raise ValueError('Capital amounts must be finite')
        if self.controller_id!='regime_switch_lp_competition': raise ValueError('Unexpected executor namespace')
        if self.live and (not self.deployment_verified or not self.account_name
                or not self.solana_wallet or not self.hyperliquid_address
                or self.race_start <= 0 or self.race_end-self.race_start != 48*3600):
            raise ValueError("Live mode requires verified Botcamp account bindings and an exact 48-hour interval")
        if self.exit_buffer_seconds < 600 or self.exit_buffer_seconds >= 3600:
            raise ValueError("End-of-race exit buffer must be 10–60 minutes")

    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self),sort_keys=True).encode()).hexdigest()
