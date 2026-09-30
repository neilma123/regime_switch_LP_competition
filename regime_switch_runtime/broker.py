"""Hummingbot API writes with public Solana/Hyperliquid reconciliation.

Credentials come from environment variables. There is no wallet key handling.
Unsigned public/quote requests are separate from explicitly armed writes.
"""
import asyncio
import math
import os
import time
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import aiohttp
import pandas as pd
from .settings import SOL,USDC,POOL
from .engine import Observation
from .regime import add_regime_features
from .profile import regime_configs

_FEATURE_CACHE={}


def hedge_limit_price(mid, buying):
    """Meet HL's five-significant-figure rule without widening the 20-bps cap."""
    price=Decimal(str(mid))*(Decimal('1.002') if buying else Decimal('.998'))
    # SOL's verified connector tick is .001. HL additionally limits decimal
    # prices to five significant figures; the tick alone is not sufficient.
    quantum=max(Decimal('.001'),Decimal(10)**(price.adjusted()-4))
    result=(price/quantum).to_integral_value(rounding=ROUND_FLOOR if buying else ROUND_CEILING)*quantum
    if not result.is_finite() or result<=0: raise ValueError('Invalid hedge limit price')
    return format(result.normalize(),'f')


class Broker:
    def __init__(self):
        self.base=os.environ.get('REGIME_API_URL','').rstrip('/')
        self.user=os.environ.get('REGIME_API_USERNAME','')
        self.password=os.environ.get('REGIME_API_PASSWORD','')
        self.rpc=os.environ.get('REGIME_SOLANA_RPC_URL','https://api.mainnet-beta.solana.com')
        self.session=None

    async def __aenter__(self):
        self.session=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self

    async def __aexit__(self,*args):
        await self.session.close()

    async def request(self,method,url,**kwargs):
        async with self.session.request(method,url,**kwargs) as r:
            if r.status>=400: raise RuntimeError(f'Endpoint returned HTTP {r.status}')
            return await r.json()

    async def api(self,method,path,**kwargs):
        if not self.base or not self.user or not self.password:
            raise ValueError('Set REGIME_API_URL, REGIME_API_USERNAME and REGIME_API_PASSWORD securely')
        return await self.request(method,self.base+path,
            auth=aiohttp.BasicAuth(self.user,self.password),**kwargs)

    async def rpc_call(self,method,params):
        result=await self.request('POST',self.rpc,json={'jsonrpc':'2.0','id':1,'method':method,'params':params})
        if 'error' in result or 'result' not in result: raise RuntimeError('Solana RPC query failed')
        return result['result']

    async def features(self):
        now=int(time.time()); end=now//3600*3600-3600
        if _FEATURE_CACHE.get('end')==end: return _FEATURE_CACHE['value']
        start=end-119*3600; candles=[]
        for first in range(start,end+1,23*3600):
            last=min(end,first+22*3600)
            data=await self.request('GET',f'https://dlmm.datapi.meteora.ag/pools/{POOL}/ohlcv',
                params={'timeframe':'1h','start_time':first,'end_time':last+3600})
            candles.extend(r for r in data['data'] if first<=r['timestamp']<=last)
        d=pd.DataFrame(candles).sort_values('timestamp').drop_duplicates('timestamp')
        if len(d)!=120 or not d.timestamp.diff().dropna().eq(3600).all():
            raise ValueError('Incomplete observed hourly history')
        for key in ('open','high','low','close','volume'):
            d[key]=pd.to_numeric(d[key],errors='raise')
            if not d[key].map(math.isfinite).all() or (d[key]<0).any():
                raise ValueError('Invalid observed candle')
        if (d[['open','high','low','close']]<=0).any().any(): raise ValueError('Invalid candle price')
        d.timestamp+=3600
        fund=await self.request('POST','https://api.hyperliquid.xyz/info',json={
            'type':'fundingHistory','coin':'SOL','startTime':start*1000,'endTime':now*1000})
        f=pd.DataFrame(fund).rename(columns={'time':'timestamp','fundingRate':'rate'})
        f['timestamp']=pd.to_numeric(f.timestamp)//1000; f['rate']=pd.to_numeric(f.rate)
        if not set(d.timestamp.iloc[:-1]).issubset(set(f.timestamp)):
            raise ValueError('Incomplete hourly funding coverage')
        reg,_=regime_configs()
        d=add_regime_features(d,f,bin_step_bps=4,config=reg)
        d['feature_ready']=d.index>=reg.lookback+reg.percentile_lookback
        # Use one fully completed bar of delay, as in the research simulator.
        row=d.iloc[-2].to_dict()
        for key in ('atr_z','hurst_pct','momentum_z','funding_z','whipsaw_pct','price_dd_fast'):
            if not math.isfinite(float(row[key])): raise ValueError('Invalid features')
        value=(int(row['timestamp']),row)
        _FEATURE_CACHE.update(end=end,value=value)
        return value

    async def public_check(self):
        stamp,row=await self.features()
        return {'status':'PUBLIC_DATA_OK','feature_timestamp':stamp,
            'feature_ready':bool(row['feature_ready']),
            'features':{k:row[k] for k in ['regime','atr_z','hurst_pct','momentum_z','funding_z','whipsaw_pct','price_dd_fast']}}

    async def observe(self,cfg):
        if not cfg.solana_wallet or not cfg.hyperliquid_address or not cfg.account_name:
            raise ValueError('Assigned wallet addresses and account profile are required for account reconciliation')
        feature_stamp,features=await self.features()
        began=time.time()
        pool,book,hl,native,usd,wrapped,positions,executors,open_orders=await asyncio.gather(
            self.api('GET','/gateway/clmm/pool-info',params={'connector':'meteora','network':'solana-mainnet-beta','pool_address':POOL}),
            self.request('POST','https://api.hyperliquid.xyz/info',json={'type':'l2Book','coin':'SOL'}),
            self.request('POST','https://api.hyperliquid.xyz/info',json={'type':'clearinghouseState','user':cfg.hyperliquid_address}),
            self.rpc_call('getBalance',[cfg.solana_wallet,{'commitment':'confirmed'}]),
            self.rpc_call('getTokenAccountsByOwner',[cfg.solana_wallet,{'mint':USDC},{'encoding':'jsonParsed','commitment':'confirmed'}]),
            self.rpc_call('getTokenAccountsByOwner',[cfg.solana_wallet,{'mint':SOL},{'encoding':'jsonParsed','commitment':'confirmed'}]),
            self.api('POST','/gateway/clmm/positions_owned',json={'connector':'meteora','network':'solana-mainnet-beta','wallet_address':cfg.solana_wallet}),
            self.api('POST','/executors/search',json={'account_names':[cfg.account_name],'limit':1000}),
            self.request('POST','https://api.hyperliquid.xyz/info',json={'type':'openOrders','user':cfg.hyperliquid_address}),
        )
        if not isinstance(positions,list) or executors['pagination'].get('has_more'):
            raise ValueError('Incomplete account position/executor inventory')
        if (pool.get('base_token_address',pool.get('baseTokenAddress'))!=SOL
                or pool.get('quote_token_address',pool.get('quoteTokenAddress'))!=USDC
                or int(pool.get('bin_step',pool.get('binStep')))!=4):
            raise ValueError('Unexpected pool mints or bin spacing')
        if max(native['context']['slot'],usd['context']['slot'],wrapped['context']['slot'])-min(native['context']['slot'],usd['context']['slot'],wrapped['context']['slot'])>32:
            raise ValueError('RPC wallet reads are not synchronized')
        def token_total(response,decimals):
            total=0
            for account in response['value']:
                amount=account['account']['data']['parsed']['info']['tokenAmount']
                if amount['decimals']!=decimals: raise ValueError('Unexpected token decimals')
                total+=int(amount['amount'])/10**decimals
            return total
        rent=0
        if positions:
            accounts=await self.rpc_call('getMultipleAccounts',[[p['position_address'] for p in positions],{'encoding':'base64','commitment':'confirmed'}])
            if any(v is None for v in accounts['value']): raise ValueError('LP rent account missing')
            rent=sum(v['lamports'] for v in accounts['value'])/1e9
        hedge_units=0.; leverage=1.; foreign=False
        for entry in hl['assetPositions']:
            p=entry['position']; amount=float(p['szi'])
            if p['coin']=='SOL':
                hedge_units+=amount; leverage=max(leverage,float(p['leverage']['value']))
            elif amount: foreign=True
        own=[]
        for e in executors['data']:
            controller=e.get('controller_id') or (e.get('config') or {}).get('controller_id')
            if controller==cfg.controller_id: own.append(e)
            elif str(e.get('status','')).upper() not in {'TERMINATED','COMPLETED'}: foreign=True
        foreign=foreign or any(p['pool_address']!=POOL for p in positions)
        if not isinstance(open_orders,list): raise ValueError('Invalid open-order inventory')
        if open_orders:
            # In particular, do not bootstrap into an account with old orders
            # merely because its currently filled position is zero.
            active_hedge=any(e.get('connector_name')=='hyperliquid_perpetual'
                and str(e.get('status','')).upper() not in {'TERMINATED','COMPLETED'} for e in own)
            if not active_hedge or any(o.get('coin')!='SOL' for o in open_orders): foreign=True
        now=int(time.time()); mid=(float(book['levels'][0][0]['px'])+float(book['levels'][1][0]['px']))/2
        age=max(time.time()-began,time.time()-int(book['time'])/1000)
        return Observation(now,float(pool['price']),mid,token_total(usd,6),
            float(native['value'])/1e9+token_total(wrapped,9),float(native['value'])/1e9,
            float(hl['marginSummary']['accountValue']),hedge_units,positions,own,rent,
            feature_stamp,features,age,foreign,leverage)

    async def quote_lp(self,cfg,obs,budget,width=.01):
        # One position account holds at most 69 bins of 4 bps: +/-1.3% is the widest fit.
        if not (math.isfinite(width) and 0<width<=.0136): raise ValueError('LP width outside the single-position bin capacity')
        lower,upper=obs.price*(1-width),obs.price*(1+width)
        # Two-percent value buffer for price changes, not permission to exceed
        # the reserved $40/$28 capital on the next tick.
        available=budget*.98
        raw=await self.api('POST','/gateway/clmm/quote-position',json={
            'connector':'meteora','network':'solana-mainnet-beta','pool_address':POOL,
            'lower_price':lower,'upper_price':upper,'base_token_amount':available/2/obs.price,
            'quote_token_amount':available/2,'slippage_pct':.1})
        # The installed Cornell API emits snake_case; newer clients may expose camelCase.
        base=float(raw.get('base_token_amount',raw.get('baseTokenAmount')))
        quote=float(raw.get('quote_token_amount',raw.get('quoteTokenAmount')))
        if not all(math.isfinite(v) and v>0 for v in (base,quote)):
            raise ValueError('Position quote is invalid or empty/one-sided')
        return dict(base_amount=base,quote_amount=quote,lower_price=lower,upper_price=upper,budget=budget,width=width)

    def spot_payload(self,amount):
        return {'type':'order_executor','connector_name':'solana-mainnet-beta',
            'trading_pair':f'{SOL}-{USDC}','side':1 if amount>0 else 2,
            'amount':str(math.floor(abs(amount)*1e9)/1e9),'execution_strategy':'MARKET',
            'slippage_pct':'.1','max_slippage_pct':'.1','slippage_multiplier':'1','leverage':1}

    def lp_payload(self,quote):
        w=float(quote.get('width') or .01)
        return {'type':'lp_executor','connector_name':'solana-mainnet-beta','lp_provider':'meteora/clmm',
            'swap_provider':'jupiter/router','pool_address':POOL,'trading_pair':f'{SOL}-{USDC}',
            'base_amount':str(quote['base_amount']),'quote_amount':str(quote['quote_amount']),
            'lower_price':str(quote['lower_price']),'upper_price':str(quote['upper_price']),
            # Executor-side stop 1.5 widths beyond each bound (+/-1.5% for the 1% profile).
            'lower_limit_price':str(quote['lower_price']*(1-1.5*w)),'upper_limit_price':str(quote['upper_price']*(1+1.5*w)),
            'side':3,'keep_position':False,'position_refresh_interval':1,
            # Bounded widening on open retries (0.1% -> 0.2% -> 0.4% -> 0.5%)
            # instead of repeating an identical tolerance until the executor fails.
            'slippage_pct':'.1','max_slippage_pct':'.5','slippage_multiplier':'2',
            'extra_params':{'strategyType':0}}

    async def hedge_payload(self,cfg,obs,change):
        # Bounded marketable limit instead of an unbounded market fill. An
        # unfilled order times out and halts; it is never assumed filled.
        limit=hedge_limit_price(obs.hedge_price,change>0)
        reducing=change*obs.hedge_units<0
        amount=min(abs(change),abs(obs.hedge_units)) if reducing else abs(change)
        if reducing and abs(change)>=abs(obs.hedge_units) and Decimal(str(amount))*Decimal(limit)<10:
            # The inspected Hummingbot base connector rejects sub-$10 orders
            # before forwarding even a CLOSE. Size the *reduce-only request*
            # to that floor; the venue may only reduce the actual position.
            # Reconcile a terminal fill to zero, not to the requested amount.
            # Acceptance/clipping on the deployed build must pass rehearsal.
            lots=(Decimal(10)/Decimal(limit)/Decimal('.01')).to_integral_value(rounding=ROUND_CEILING)
            amount=float(lots*Decimal('.01'))
        return {'type':'order_executor','connector_name':'hyperliquid_perpetual','trading_pair':'SOL-USD',
            'side':1 if change>0 else 2,'amount':str(round(amount,8)),
            'position_action':'CLOSE' if reducing else 'OPEN','execution_strategy':'LIMIT',
            'price':str(limit),'leverage':1}

    async def submit(self,cfg,ident,payload):
        if not cfg.live: raise ValueError('Broker writes require live mode')
        result=await self.api('POST','/executors/',json={'account_name':cfg.account_name,
            'controller_id':cfg.controller_id,'executor_config':{**payload,'id':ident,'controller_id':cfg.controller_id,'timestamp':time.time()}})
        if str(result.get('executor_id') or result.get('id'))!=ident:
            raise ValueError('Backend did not retain the persisted executor ID')
        return result

    async def stop(self,ident):
        if not ident: raise ValueError('Cannot stop an unowned executor')
        return await self.api('POST',f'/executors/{ident}/stop',json={'keep_position':False})

    async def close_position(self,cfg,address):
        if not cfg.live or not address: raise ValueError('Owned LP recovery requires live mode and an address')
        return await self.api('POST','/gateway/clmm/close',json={
            'connector':'meteora','network':'solana-mainnet-beta','pool_address':POOL,
            'position_address':address,'wallet_address':cfg.solana_wallet,'slippage_pct':.1})
