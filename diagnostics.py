"""Explain the screening funnel without changing strategy parameters."""
import json
from collections import Counter
import pandas as pd
from strategy import signal,eligible,entry_mode


def screening(data,universe,cfg,start,end):
    stages={k:0 for k in ['eligible_stock_days','warmup_ready','has_support_resistance','touch_support','close_back_above','rsi_at_least_30','gross_rr_at_close_at_least_threshold','conditional_candidates']}
    mode=entry_mode(cfg)
    if mode=='support_touch':
        for k in ['close_back_above','rsi_at_least_30','gross_rr_at_close_at_least_threshold']:del stages[k]
    examples=[];latest=[]
    for m in universe['constituents']:
        s=m['symbol'];f=data[s]
        for day,r in f.loc[start:end].iterrows():
            ds=str(day.date())
            if not eligible(m,ds):continue
            stages['eligible_stock_days']+=1
            if r.history_count<cfg['minimum_history']:continue
            stages['warmup_ready']+=1
            if not r.support<r.resistance:continue
            stages['has_support_resistance']+=1
            S,R,A=r.support,r.resistance,r.atr_prev
            if not S-.5*A<=r.low<=S+.5*A:continue
            stages['touch_support']+=1
            if mode=='close_confirmation':
                if not S<r.close<=S+A:continue
                stages['close_back_above']+=1
                if not (r.rsi>=30):continue
                stages['rsi_at_least_30']+=1
                rr=(R-r.close)/(r.close-S+cfg['stop_atr']*A)
                if rr>=cfg['min_rr']:stages['gross_rr_at_close_at_least_threshold']+=1
            candidate=signal(r,cfg)
            if candidate:
                stages['conditional_candidates']+=1
                examples.append({'symbol':s,'date':ds,**candidate})
        r=f.loc[:end].iloc[-1]
        if eligible(m,end):
            value=lambda k:None if pd.isna(r[k]) else float(r[k]/r.factor)
            latest.append({'symbol':s,'name':m['name'],'sector':m['sector'],'date':str(r.name.date()),'close':float(r.raw_close),'rsi':float(r.rsi),'atr':value('atr'),'support':value('support'),'resistance':value('resistance'),'history_count':int(r.history_count)})
    return {'entry_mode':mode,'min_rr':cfg['min_rr'] if mode=='close_confirmation' else None,'period':[start,end],'stages':stages,'candidate_details':examples,'latest_scan':latest,
            'note':'股日為每檔股票每個交易日；最後一天候選尚無下一交易日成交資料，不算回測交易。'}


def audit_candidates(data,universe,cfg,result):
    """Reconcile every raw signal with holdings, next-session fills or skips."""
    summary,curve,trades,closed,skips=result
    start=pd.Timestamp(summary['start']);end=pd.Timestamp(summary['end'])
    calendar=data['0050'].index
    previous=calendar[calendar<start]
    first=previous[-1] if len(previous) else start
    intervals=list(closed)+[dict(p,symbol=s,exit_date='9999-12-31') for s,p in summary['open_positions'].items()]
    buys={(t['symbol'],t['date']):t for t in trades if t['side']=='BUY'}
    rejected={(t['symbol'],t['date']):t for t in skips}
    rows=[]
    for meta in universe['constituents']:
        symbol=meta['symbol']
        for date,row in data[symbol].loc[first:end].iterrows():
            ds=str(date.date())
            if not eligible(meta,ds) or signal(row,cfg) is None:continue
            future=calendar[calendar>date]
            next_date=str(future[0].date()) if len(future) else None
            item={'symbol':symbol,'name':meta['name'] if 'name' in meta else symbol,'signal_date':ds,'execution_date':next_date}
            if any(p['symbol']==symbol and p['entry_date']<=ds<p['exit_date'] for p in intervals):
                item.update(outcome='already_held',reason='訊號日收盤已持有；不加碼，不另開新倉')
            elif next_date is None or next_date>summary['end']:
                item.update(outcome='outside_period',reason='下一交易日不在回測期間內')
            elif (symbol,next_date) in buys:
                t=buys[symbol,next_date]
                item.update(outcome='bought',reason=t['reason'],quantity=t['quantity'],price=t['price'])
            elif (symbol,next_date) in rejected:
                reason=rejected[symbol,next_date]['reason']
                item.update(outcome='gap_cancel' if reason=='開盤跌破或等於支撐，當日取消' else 'other_cancel',reason=reason)
            elif not eligible(meta,next_date):
                item.update(outcome='other_cancel',reason='隔日已不在股票池')
            else:
                raise AssertionError(f'候選未能對帳：{symbol} {ds}')
            rows.append(item)
    rows.sort(key=lambda x:(x['signal_date'],x['symbol']))
    counts=dict(Counter(x['outcome'] for x in rows))
    assert counts.get('bought',0)==summary['buys'],'候選與買進筆數不一致'
    return {'first_signal_date':str(first.date()),'last_signal_date':str(end.date()),'total':len(rows),'counts':counts,'rows':rows,
            'note':'候選按股票×訊號日計數，含回測首日前一交易日；已持有的重複訊號仍列入總數，但不加碼。'}
