import copy
import json
import pandas as pd
from strategy import signal,manage,size,new_position,commission,eligible,entry_mode,entry_rules


def make_plan(data,events,universe,account,cfg,as_of):
    date=pd.Timestamp(as_of);ds=str(date.date());meta={x['symbol']:x for x in universe['constituents']}
    if date not in data['0050'].index:raise ValueError('指定日期不是已下載的 0050 交易日')
    if account['as_of']>ds:raise ValueError('庫存日期晚於分析日期')
    if ds>universe['as_of']:raise ValueError('成分股名單比分析日舊，請先更新名單')
    working=copy.deepcopy(account);actions=[];warnings=[];marks={};stale_held=False
    for sym,p in working['positions'].items():
        if sym not in meta:
            meta[sym]={'symbol':sym,'name':sym,'sector':p.get('sector','UNKNOWN')}
            if cfg.get('max_sector_fraction') is not None and 'sector' not in p:p['mode']='unmanaged'
    for sym in meta:
        if sym not in data:raise ValueError(f'{sym} 無行情；庫存不允許漏估值，請先下載')
        previous=data[sym].loc[:date]
        if previous.empty:raise ValueError(f'{sym} 無可用估值')
        marks[sym]=float(previous.iloc[-1].raw_close)
        if previous.index[-1]!=date:
            warnings.append(f'{sym}: 當日無行情，不產生新進場')
            if sym in working['positions']:stale_held=True
    equity=working['cash']+sum(p['quantity']*marks[s] for s,p in working['positions'].items())
    for sym,p in working['positions'].items():
        last=p.get('last_managed_date') or p.get('entry_date') or working['as_of']
        basis=p.get('basis_date',last)
        # A reported quantity is authoritative: never auto-credit bonus shares.
        for ed,e in sorted(events[sym].items()):
            if basis<ed<=ds:
                p['mode']='unmanaged';warnings.append(f'{sym} {ed}: 庫存跨公司行動，需回報實際股數/成本並重新確認管理價格')
        # A newly reported fill must also be checked against its entry-day close.
        # Once managed, only subsequent sessions are processed (idempotent plans).
        after=(data[sym].index>pd.Timestamp(last)) if p.get('last_managed_date') else (data[sym].index>=pd.Timestamp(last))
        rows=data[sym].loc[after&(data[sym].index<=date)]
        action='REVIEW' if p.get('mode')=='unmanaged' else ('SELL' if p.get('pending_exit') else 'HOLD')
        reason=p.get('pending_exit') or '沿用既有管理狀態'
        for d,row in rows.iterrows():
            action,p,reason=manage(p,row,cfg);p['last_managed_date']=str(d.date())
        if p.get('mode')=='unmanaged':action='REVIEW';reason='缺少可核實的原始交易設定或公司行動對帳'
        working['positions'][sym]=p
        actions.append({'symbol':sym,'name':meta[sym]['name'],'action':action,'quantity':p['quantity'] if action=='SELL' else 0,
                        'held_quantity':p['quantity'],'sell_fraction':1.0 if action=='SELL' else 0.,'position_weight':p['quantity']*marks[sym]/equity,
                        'stop':p.get('stop'),'resistance':p.get('resistance'),'reason':reason})
    candidates=[];excluded=[]
    for sym,m in meta.items():
        if sym in working['positions'] or sym not in {x['symbol'] for x in universe['constituents']} or not eligible(m,ds):continue
        if date not in data[sym].index:continue
        row=data[sym].loc[date]
        if row.history_count<cfg['minimum_history']:
            excluded.append({'symbol':sym,'reason':f'有效暖機 {int(row.history_count)} 根，要求 {cfg["minimum_history"]} 根'});continue
        candidate=signal(row,cfg)
        if candidate:candidates.append(dict(candidate,symbol=sym,name=m['name'],sector=m.get('sector'),signal_date=ds))
    candidates.sort(key=lambda x:(-x['rr'],-x['avg_volume'],x['symbol']))
    reserved=copy.deepcopy(working['positions']);cash=working['cash']
    stale=(date-pd.Timestamp(working['as_of'])).days>4 or stale_held
    if stale:warnings.append('庫存快照超過四個日曆日或持股缺當日行情，暫停新倉數量；先對帳')
    for c in candidates:
        q,reason=size(c,c['reference_price'],equity,cash,reserved,marks,meta,cfg) if not stale else (0,'庫存過舊')
        c.update({'suggested_quantity':q,'suggested_amount':q*c['reference_price'],'suggested_weight':q*c['reference_price']/equity,'reason':reason,'status':'CONDITIONAL_BUY' if q else 'WATCH'})
        if q:
            cash-=q*c['reference_price']+commission(q*c['reference_price'],cfg)
            reserved[c['symbol']]=new_position(c,q,c['reference_price'],ds,cfg)
    working['last_plan_date']=ds
    plan={'as_of':ds,'for_session':'下一個台股交易日','account_as_of':working['as_of'],'cash':working['cash'],'cash_is_approximate':working.get('cash_is_approximate',False),
          'equity':equity,'estimated_remaining_cash':cash,'actions':actions,'candidates':candidates,'excluded':excluded,'warnings':warnings,
          'entry_mode':entry_mode(cfg),'entry_rules':entry_rules(cfg),
          'instructions':['所有買單是條件單：開盤價與下單前現價都必須高於原支撐，且現價不高於最高買價',f"最高買價為未扣成本的理論上限；仍須通過依實際價格與股數計算的淨報酬風險比 ≥ {cfg['min_rr']:g}",'開盤跌破或等於支撐，當天整筆取消；盤中收回也不重啟','隔夜公司行動取消舊買單；價格／股數先重新核對','股數及比例是依收盤價試算；下單前用 quotes 指令重算','賣出現金尚未回報入帳前，不用來配置新買單','只產生建議，不串券商，也不把建議當成成交']}
    if entry_mode(cfg)=='support_touch':
        plan['instructions'][0]='觸及支撐區後隔日才可買；開盤價與下單前現價都必須高於原支撐'
        plan['instructions'][1]='本觸及支撐版不檢查 RSI、收盤確認或報酬風險比，沒有報酬風險比推算的最高買價；仍遵守停損與資金限制'
    sector='不限制、不使用產業分類' if cfg.get('max_sector_fraction') is None else f"上限 {cfg['max_sector_fraction']:.0%}"
    plan['instructions'].append(f"配置：單股上限 {cfg['max_position_fraction']:.0%}；每筆初始風險 {cfg['risk_fraction']:.0%}；整體初始風險 {cfg['max_total_risk_fraction']:.0%}；產業：{sector}；不加碼")
    return plan,working


def opening_check(plan,quotes,account,universe,cfg):
    if quotes['signal_date']!=plan['as_of']:raise ValueError('報價對應的訊號日期不符')
    trade_date=quotes['trade_date']
    if not plan['as_of']<trade_date:raise ValueError('報價日期必須在訊號日期之後')
    if (pd.Timestamp(trade_date)-pd.Timestamp(plan['as_of'])).days>7:raise ValueError('訊號已過期')
    if quotes.get('is_next_trading_session') is not True:raise ValueError('請確認 is_next_trading_session:true；本功能不猜測國定假日')
    if account['as_of']<plan['account_as_of'] or (pd.Timestamp(trade_date)-pd.Timestamp(account['as_of'])).days>5:raise ValueError('請先更新庫存／現金')
    meta={x['symbol']:x for x in universe['constituents']};positions=copy.deepcopy(account['positions']);cash=account['cash']
    live=quotes['quotes'];marks={s:float(x['price']) for s,x in live.items()}
    if set(positions)-set(marks):raise ValueError('請提供全部持倉的最新報價，以便重新估值')
    for s,p in positions.items():
        if s not in meta:meta[s]={'sector':p.get('sector','UNKNOWN')}
    equity=cash+sum(p['quantity']*marks[s] for s,p in positions.items());result=[]
    for c in plan['candidates']:
        s=c['symbol'];q=0;reason='缺少報價';price=None
        if s in positions:reason='已持有，不加碼'
        elif s in live:
            x=live[s];price=float(x['price']);opening=float(x['open'])
            if not 0<price or not 0<opening:raise ValueError('價格必須大於零')
            if x.get('corporate_action',False):reason='公司行動，取消舊買單'
            elif opening<=c['support']:reason='開盤跌破或等於支撐，當日取消'
            elif not eligible(meta[s],trade_date):reason='已不在股票池'
            else:q,reason=size(c,price,equity,cash,positions,marks,meta,cfg)
        if q:
            cash-=q*price+commission(q*price,cfg);positions[s]=new_position(c,q,price,trade_date,cfg)
        result.append({'symbol':s,'name':c['name'],'action':'BUY_ELIGIBLE' if q else 'CANCEL_OR_WAIT','quantity':q,'price':price,'weight':q*price/equity if q else 0,'reason':reason})
    return {'trade_date':trade_date,'signal_date':plan['as_of'],'equity':equity,'remaining_cash':cash,'orders':result,'note':'人工提供報價的當下檢查；非即時行情訂閱、非已成交紀錄'}


def markdown(plan):
    lines=['# 隔日交易清單',f"訊號日：{plan['as_of']}；用途：{plan['for_session']}。",f"庫存日：{plan['account_as_of']}；可用現金：{plan['cash']:,.0f} 元；淨值：{plan['equity']:,.0f} 元。",'']
    if plan['cash_is_approximate']:lines+=['現金為使用者提供的約略值；下單前需以券商實際可用額度更新。','']
    lines+=['## 買進候選','', '| 代號 | 名稱 | 狀態 | 試算股數 | 淨值比例 | 支撐 | 最高買價 | 停損 | 壓力 | 說明 |','|---|---|---|---:|---:|---:|---:|---:|---:|---|']
    for c in plan['candidates']:
        max_text='無（仍須高於支撐）' if c['max_buy_price'] is None else f"{c['max_buy_price']:.2f}"
        lines.append(f"| {c['symbol']} | {c['name']} | {c['status']} | {c['suggested_quantity']} | {c['suggested_weight']:.2%} | {c['support']:.2f} | {max_text} | {c['stop']:.2f} | {c['resistance']:.2f} | {c['reason']} |")
    if not plan['candidates']:lines+=['','今天沒有符合條件的買進候選，保留現金。']
    lines+=['','## 庫存與賣出','']
    if not plan['actions']:lines+=['目前空手，沒有可賣出的股票。']
    for a in plan['actions']:lines.append(f"- {a['symbol']} {a['name']}：{a['action']}；庫存 {a['held_quantity']} 股，建議賣出 {a['quantity']} 股（庫存的 {a['sell_fraction']:.0%}）；{a['reason']}。")
    lines+=['','## 執行條件','']+['- '+x for x in plan['instructions']]
    lines+=['','## 資料與限制','']+['- '+x for x in plan['warnings']]+['- '+x['symbol']+'：'+x['reason'] for x in plan['excluded']]
    return '\n'.join(lines)
