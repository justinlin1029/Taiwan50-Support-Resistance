"""Numerical adaptation of Zeiierman's Buyers & Sellers Profile + Dynamic S/R.

Original © Zeiierman, CC BY-NC-SA 4.0. Python adaptation, 2026-10-17.
https://www.tradingview.com/script/bEcZR3nB-Buyers-Sellers-Profile-Dynamic-S-R-Zeiierman/
https://creativecommons.org/licenses/by-nc-sa/4.0/
Drawing code omitted. Split/ex-dividend normalization and warmup are added.
Not certified as bit-identical to TradingView; see README for differences.
"""
from dataclasses import dataclass
import numpy as np
import pandas as pd


def rma(x, n=14):
    """Wilder average, seeded with first n nonmissing values (not pandas EWM seed)."""
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    valid = np.flatnonzero(np.isfinite(x))
    if len(valid) < n:
        return out
    first = valid[n-1]
    out[first] = x[valid[:n]].mean()
    for i in range(first+1, len(x)):
        out[i] = (out[i-1]*(n-1)+x[i])/n if np.isfinite(x[i]) else out[i-1]
    return out


def series(frame):
    h,l,c = [frame[k].to_numpy(float) for k in ['high','low','close']]
    prev = np.r_[c[0], c[:-1]]
    atr = rma(np.maximum.reduce([h-l, np.abs(h-prev), np.abs(l-prev)]))
    delta = np.r_[np.nan, np.diff(c)]
    gain, loss = rma(np.maximum(delta,0)), rma(np.maximum(-delta,0))
    with np.errstate(divide='ignore',invalid='ignore'):
        rsi = 100-100/(1+gain/loss)
    rsi[(gain==0)&(loss==0)] = 50
    return atr, rsi


def profile(h, l, c, v, rows, tick):
    lo, hi = l.min(), h.max()
    span = max(hi-lo, tick*rows)
    bot = lo-(span-(hi-lo))/2
    step = span/rows
    lower = bot+np.arange(rows)*step
    ranges = h-l
    overlap = np.maximum(0, np.minimum(h[:,None],lower+step)-np.maximum(l[:,None],lower))
    frac = np.divide(overlap,ranges[:,None],out=np.zeros_like(overlap),where=ranges[:,None]>0)
    zero = ranges==0
    if zero.any():
        idx = np.clip(np.floor((l[zero]-bot)/step).astype(int),0,rows-1)
        frac[np.flatnonzero(zero),idx] = 1
    buy_share = np.divide(c-l,ranges,out=np.full_like(c,0.5),where=ranges>0).clip(0,1)
    allocation = frac*np.maximum(v,0)[:,None]
    return bot,step,allocation.sum(axis=0),(allocation*buy_share[:,None]).sum(axis=0)


@dataclass
class Level:
    price: float
    score: float
    support: bool
    confirmed: int
    pivot: int


def compute(frame, cfg):
    """Input normalized OHLC; output reference levels in same price basis.
    Every row only depends on its prefix. Today's reference S/R is yesterday's
    active state; today's new pivots are available only to tomorrow's signal.
    """
    f=frame.copy()
    o,h,l,c,v=[f[k].to_numpy(float) for k in ['open','high','low','close','volume']]
    atr,rsi=series(f)
    avg=pd.Series(v).rolling(40).mean().to_numpy()
    n=len(f); swing=cfg['swing_length']; levels=[]
    sup=np.full(n,np.nan);res=np.full(n,np.nan)
    sscore=np.full(n,np.nan); rscore=np.full(n,np.nan)
    counts=np.zeros(n,int)
    # TradingView minimum tick is usually .01 for these symbols. Scale to basis.
    ticks=f.get('factor',pd.Series(1.,index=f.index)).to_numpy(float)*0.01
    clip=lambda x: float(np.clip(x,0,1))
    for t in range(n):
        if t:
            ss=[x for x in levels if x.support and x.price<c[t-1]]
            rr=[x for x in levels if not x.support and x.price>c[t-1]]
            if ss:
                best=max(ss,key=lambda x:x.price);sup[t]=best.price;sscore[t]=best.score
            if rr:
                best=min(rr,key=lambda x:x.price);res[t]=best.price;rscore[t]=best.score
        p=t-swing
        if t>40+swing*2+5 and p>=swing:
            # Rightmost equal extremum wins; no future beyond confirmation t.
            is_low=l[p]<=l[p-swing:p].min() and l[p]<l[p+1:t+1].min()
            is_high=h[p]>=h[p-swing:p].max() and h[p]>h[p+1:t+1].max()
            if is_low or is_high:
                start=max(0,p-cfg['profile_lookback']+1)
                bot,step,tot,buy=profile(h[start:p+1],l[start:p+1],c[start:p+1],v[start:p+1],cfg['profile_rows'],ticks[p])
                mx=max(tot.max(),1e-7)
                ap=max(atr[p] if np.isfinite(atr[p]) else ticks[p],ticks[p])
                for supp in [True,False]:
                    if not (is_low if supp else is_high):continue
                    raw=l[p] if supp else h[p]
                    rad=max(ap*cfg['search_atr'],step*2)
                    candidates=[]
                    for j,rv in enumerate(tot):
                        price=bot+(j+.5)*step;dist=abs(price-raw)
                        if dist>rad:continue
                        strength=rv/mx
                        neighbors=max(((tot[j-1] if j else rv)+(tot[j+1] if j+1<len(tot) else rv))/2,mx*.01)
                        peak=clip((rv/neighbors-.85)/.65)
                        share=clip(buy[j]/rv) if rv>0 else .5
                        direct=share if supp else 1-share
                        evidence=.5*strength+.2*peak+.2*direct+.1*(1-dist/rad)
                        candidates.append((evidence,price,strength))
                    if not candidates:continue
                    evidence,node,strength=max(candidates,key=lambda x:x[0])
                    if strength<cfg['min_node'] or evidence<cfg['min_evidence']:continue
                    vr=v[p]/avg[p] if avg[p]>0 else 1
                    volume_score=clip((vr-.8)/1.4)
                    pr=max(h[p]-l[p],ticks[p])
                    wick=min(o[p],c[p])-l[p] if supp else h[p]-max(o[p],c[p])
                    wick_score=clip(wick/pr/.5)
                    absorption=0.
                    radius=min(3,swing)
                    for k in range(p-radius,p+radius+1):
                        cr=max(h[k]-l[k],ticks[k]);br=abs(c[k]-o[k])/cr
                        vr=v[k]/avg[k] if avg[k]>0 else 1
                        absorption=max(absorption,np.sqrt(clip((.4-br)/.4)*clip((vr-1)/.5)))
                    score=100*(.5*evidence+.2*absorption+.15*volume_score+.15*wick_score)
                    if score<cfg['min_score']:continue
                    price=raw+(node-raw)*cfg['profile_pull']*clip(evidence/.65)
                    new=Level(price,score,supp,t,p)
                    close=[(abs(x.price-price),i) for i,x in enumerate(levels) if x.support==supp and abs(x.price-price)<=max(ap*cfg['merge_atr'],ticks[p])]
                    if close:
                        _,i=min(close)
                        if score>levels[i].score:levels[i]=new
                    else:levels.append(new)
        if np.isfinite(atr[t]):
            buf=atr[t]*cfg['level_break_atr']
            levels=[x for x in levels if not (c[t]<x.price-buf if x.support else c[t]>x.price+buf)]
        while len(levels)>cfg['max_levels']:
            levels.pop(min(range(len(levels)),key=lambda i:levels[i].score))
        counts[t]=len(levels)
    f['atr']=atr;f['rsi']=rsi;f['support']=sup;f['resistance']=res
    f['support_score']=sscore;f['resistance_score']=rscore;f['active_levels']=counts
    f['atr_prev']=pd.Series(atr,index=f.index).shift(1)
    f['rsi_prev']=pd.Series(rsi,index=f.index).shift(1)
    f['volume_mean20']=f['volume'].rolling(20).mean()
    f['history_count']=np.arange(n)+1
    return f
