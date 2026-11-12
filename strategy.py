"""Shared daily signals and portfolio-aware sizing. No broker connection."""
import copy
import math
import numpy as np


def commission(amount,cfg):
    return max(cfg['minimum_commission'],amount*cfg['commission_rate']) if amount>0 else 0.


def tick(price,etf=False):
    if etf:return .01 if price<50 else .05
    for upper,step in [(10,.01),(50,.05),(100,.1),(500,.5),(1000,1),(float('inf'),5)]:
        if price<upper:return step


def round_price(price,up=False,etf=False):
    step=tick(price,etf)
    return round((math.ceil(price/step-1e-9) if up else math.floor(price/step+1e-9))*step,4)


def eligible(meta,date):
    return meta.get('valid_from','0000')<=date<=meta.get('valid_to','9999')


def entry_mode(cfg):
    mode=cfg.get('entry_mode','close_confirmation')
    if mode not in ['close_confirmation','support_touch']:
        raise ValueError('entry_mode 必須是 close_confirmation 或 support_touch')
    return mode


def entry_rules(cfg):
    if entry_mode(cfg)=='support_touch':
        return '觸及支撐區即列隔日候選；不要求收盤確認、RSI 或報酬風險比；仍需同時有支撐壓力，且隔日買價高於支撐'
    return f"收盤守住支撐且不超過支撐＋1 ATR；RSI ≥ 30；配置後扣成本報酬風險比 ≥ {cfg['min_rr']:g}"


def signal(row,cfg):
    mode=entry_mode(cfg)
    if row.history_count<cfg['minimum_history']:return None
    factor=row.factor
    s,r,a=[float(row[k]/factor) for k in ['support','resistance','atr_prev']]
    c=float(row.raw_close);low=float(row.raw_low)
    if not all(np.isfinite([s,r,a,c,low])) or a<=0 or not s<r:return None
    if not s-.5*a<=low<=s+.5*a:return None
    if mode=='close_confirmation' and not (s<c<=s+a and np.isfinite(row.rsi) and row.rsi>=30):return None
    stop=s-cfg['stop_atr']*a
    if stop<=0:return None
    # Gross RR gate; final net RR includes exact commissions for allocated qty.
    max_price=None
    if mode=='close_confirmation':
        max_price=round_price((r+cfg['min_rr']*stop)/(1+cfg['min_rr']))
        if max_price<=s:return None
    return {'support':s,'resistance':r,'stop':stop,'atr':a,'reference_price':c,
            'max_buy_price':max_price,'rsi':float(row.rsi) if np.isfinite(row.rsi) else None,'rr':(r-c)/(c-stop) if c>stop else -1.,'entry_mode':mode,
            'avg_volume':float(row.volume_mean20*row.volume_factor),
            'support_score':float(row.support_score)}


def manage(position,row,cfg):
    """Return next-session instruction; update state once per daily close."""
    p=copy.deepcopy(position)
    if p.get('mode')=='unmanaged':return 'REVIEW',p,'缺少原始支撐、壓力或停損，先補齊管理設定'
    if p.get('pending_exit'):return 'SELL',p,p['pending_exit']
    if row.raw_close<=p['stop']:
        p['pending_exit']='收盤觸及停損';return 'SELL',p,p['pending_exit']
    p['highest_close']=max(p.get('highest_close',row.raw_close),float(row.raw_close))
    a=float(row.atr_prev/row.factor)
    if p['mode']=='range':
        if row.raw_close>p['resistance']+cfg['breakout_atr']*a:
            p['mode']='trend'
        elif row.raw_high>=p['resistance']-.5*a and row.raw_close<=p['resistance']:
            p['pending_exit']='測試壓力後收回壓力以下';return 'SELL',p,p['pending_exit']
    if p['mode']=='trend':p['stop']=max(p['stop'],p['highest_close']-cfg['trail_atr']*float(row.atr/row.factor))
    return 'HOLD',p,'突破續抱' if p['mode']=='trend' else '區間持有'


def size(candidate,price,equity,cash,positions,marks,metadata,cfg):
    """Allocate sequentially, so multiple signals share cash/sector/risk caps."""
    mode=entry_mode(cfg)
    maximum=candidate['max_buy_price']
    if not np.isfinite(price) or price<=candidate['support'] or (mode=='close_confirmation' and (maximum is None or price>maximum)):
        return 0,'價格不在支撐上方的可買區間'
    if any(p.get('mode')=='unmanaged' for p in positions.values()):return 0,'有未設定風險的庫存，暫停新倉'
    if equity<=0:return 0,'淨值不足'
    # None disables sector grouping entirely, including metadata lookups.
    sector_amount=float('inf')
    if cfg.get('max_sector_fraction') is not None:
        sector=metadata[candidate['symbol']]['sector']
        current_sector=sum(p['quantity']*marks[s] for s,p in positions.items() if metadata[s]['sector']==sector)
        sector_amount=equity*cfg['max_sector_fraction']-current_sector
    remaining_risk=equity*cfg['max_total_risk_fraction']-sum(p.get('initial_risk',0) for p in positions.values())
    # Allow estimated exit slippage, commissions and tax in initial risk budget.
    exit_price=max(0,candidate['stop']*(1-cfg['slippage_bps']/10000))
    per_share_risk=price-exit_price+price*cfg['commission_rate']+exit_price*(cfg['commission_rate']+cfg['sell_tax'])
    budget=min(equity*cfg['risk_fraction'],remaining_risk)
    max_amount=min(equity*cfg['max_position_fraction'],sector_amount,cash)
    if min(budget,max_amount)<=0:
        reason='現金／單股／組合風險額度不足' if cfg.get('max_sector_fraction') is None else '現金／單股／產業／組合風險額度不足'
        return 0,reason
    q=int(min(budget/per_share_risk,max_amount/price,candidate['avg_volume']*cfg['max_volume_fraction']))
    lot=cfg['lot_size'];q=q//lot*lot
    if lot==1:q=min(q,999) # One odd-lot order; don't undercount mixed-board fees.
    while q>0:
        amount=q*price;buy_fee=commission(amount,cfg)
        risk=q*(price-exit_price)+buy_fee+commission(q*exit_price,cfg)+q*exit_price*cfg['sell_tax']
        if amount+buy_fee<=cash+1e-8 and risk<=budget+1e-8:break
        q-=lot
    if q<=0:return 0,'最小交易單位或最低手續費超過配置額度'
    target=candidate['resistance']*(1-cfg['slippage_bps']/10000)
    reward=q*(target-price)-commission(q*price,cfg)-commission(q*target,cfg)-q*target*cfg['sell_tax']
    if mode=='close_confirmation' and reward/risk<cfg['min_rr']:return 0,'扣除成本後報酬風險比不足'
    return q,'符合配置限制'


def new_position(candidate,q,price,date,cfg):
    exit_price=candidate['stop']*(1-cfg['slippage_bps']/10000)
    risk=q*(price-exit_price)+commission(q*price,cfg)+commission(q*exit_price,cfg)+q*exit_price*cfg['sell_tax']
    return {'quantity':q,'avg_cost':price,'entry_date':date,'support':candidate['support'],
            'resistance':candidate['resistance'],'stop':candidate['stop'],'initial_stop':candidate['stop'],
            'initial_risk':risk,'mode':'range','highest_close':price,'pending_exit':None,
            'entry_cost':q*price+commission(q*price,cfg),'dividends':0.,'last_managed_date':None,
            'basis_date':date}


def adjust_position(p,event):
    """Mechanical corporate-action adjustment is not discretionary stop widening."""
    out=copy.deepcopy(p)
    for k in ['support','resistance','stop','initial_stop','highest_close']:
        if k in out:out[k]/=event['factor']
    out['quantity']*=event['split']
    out['avg_cost']/=event['split']
    return out
