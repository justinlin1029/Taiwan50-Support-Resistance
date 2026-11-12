import copy
import json
from pathlib import Path
import pandas as pd
from metrics import sharpe_ratio
from strategy import signal,manage,size,new_position,adjust_position,commission,round_price,eligible,entry_mode,entry_rules


def run(data,events,universe,cfg,start,end):
    start,end=pd.Timestamp(start),pd.Timestamp(end)
    benchmark=data['0050']
    sessions=benchmark.loc[start:end].index
    if not len(sessions):raise ValueError('回測區間沒有交易日')
    if (end-sessions[-1]).days>4:raise ValueError('價格資料過舊，拒絕當成完整回測')
    meta={x['symbol']:x for x in universe['constituents']}
    missing=set(meta)-set(data)
    if missing:raise ValueError(f'缺少行情: {sorted(missing)}')
    cash=float(cfg['initial_cash']);positions={};pending=[];ledger=[];curve=[];marks={};receivables=[];closed=[];warnings=[];skips=[]
    bq=0.;bcash=cash;breceivables=[]
    # First test-day orders may use the immediately preceding completed session.
    prev=benchmark.index[benchmark.index<sessions[0]]
    if len(prev):
        date=prev[-1]
        for sym in meta:
            if date in data[sym].index and eligible(meta[sym],str(date.date())):
                sig=signal(data[sym].loc[date],cfg)
                if sig:pending.append(dict(sig,symbol=sym,signal_date=str(date.date())))
    for date in sessions:
        ds=str(date.date())
        for item in list(receivables):
            if item['pay_date'] and item['pay_date']<=ds:cash+=item['amount'];receivables.remove(item)
        for item in list(breceivables):
            if item['pay_date'] and item['pay_date']<=ds:bcash+=item['amount'];breceivables.remove(item)
        # Entitlement belongs to holders before ex-date, not today's buyers.
        for sym,p in list(positions.items()):
            e=events[sym].get(ds)
            if e:
                if e['unsupported']:raise ValueError(f'{ds} {sym} 持倉遇到未支援公司行動；需補齊權利價值，停止回測')
                amount=p['quantity']*e['cash']
                if amount:
                    receivables.append({'amount':amount,'pay_date':e['pay_date'],'symbol':sym});p['dividends']+=amount
                    ledger.append({'date':ds,'symbol':sym,'side':'DIVIDEND_RECEIVABLE','quantity':p['quantity'],'price':e['cash'],'amount':amount,'reason':'除息權利；到發放日才可用'})
                if e.get('bonus'):warnings.append(f'{ds} {sym}: 股票股利暫以除權日可交易估算，實際入帳可能較晚')
                positions[sym]=adjust_position(p,e)
        e=events['0050'].get(ds)
        if e and bq:
            if e['unsupported']:raise ValueError('0050 基準遇到不明公司行動')
            if e['cash']:breceivables.append({'amount':bq*e['cash'],'pay_date':e['pay_date']})
            bq*=e['split']
        if bq==0:
            price=round_price(float(benchmark.loc[date].raw_open)*(1+cfg['slippage_bps']/10000),up=True,etf=True)
            bq=int((bcash-cfg['minimum_commission'])/(price*(1+cfg['commission_rate'])))
            bcash-=bq*price+commission(bq*price,cfg)
        # Opening marks only; never today's close for position sizing.
        for sym in meta:
            f=data[sym]
            if date in f.index:marks[sym]=float(f.loc[date].raw_open)
            elif sym not in marks:
                previous=f.loc[f.index<date]
                if len(previous):marks[sym]=float(previous.iloc[-1].raw_close)
        # Pending exits stay pending if suspended / flat limit-down proxy.
        for sym,p in list(positions.items()):
            if not p.get('pending_exit') or date not in data[sym].index:continue
            row=data[sym].loc[date];prior=data[sym].loc[data[sym].index<date]
            if not len(prior):continue
            reference=float(prior.iloc[-1].raw_close)/events[sym].get(ds,{}).get('factor',1)
            if row.raw_high==row.raw_low and row.raw_close<=reference*.905:continue
            q=min(p['quantity'],int(row.raw_volume*cfg['max_volume_fraction']))
            if q<=0:continue
            price=round_price(float(row.raw_open)*(1-cfg['slippage_bps']/10000))
            amount=q*price;fee=commission(amount,cfg);tax=amount*cfg['sell_tax'];cash+=amount-fee-tax
            frac=q/p['quantity'];cost=p['entry_cost']*frac;div=p.get('dividends',0)*frac
            pnl=amount-fee-tax-cost+div
            ledger.append({'date':ds,'symbol':sym,'side':'SELL','quantity':q,'price':price,'amount':amount,'fee':fee,'tax':tax,'pnl':pnl,'reason':p['pending_exit']})
            if frac>=1-1e-10:
                closed.append({'symbol':sym,'entry_date':p['entry_date'],'exit_date':ds,'pnl':pnl,'return':pnl/cost});del positions[sym]
            else:
                p['quantity']-=q;p['entry_cost']*=1-frac;p['initial_risk']*=1-frac;p['dividends']*=1-frac
        equity=cash+sum(x['amount'] for x in receivables)+sum(p['quantity']*marks[s] for s,p in positions.items())
        for candidate in sorted(pending,key=lambda x:(-x['rr'],-x['avg_volume'],x['symbol'])):
            sym=candidate['symbol']
            if sym in positions or not eligible(meta[sym],ds):continue
            if date not in data[sym].index:
                skips.append({'date':ds,'symbol':sym,'reason':'無交易價格；取消當日買單'});continue
            row=data[sym].loc[date];candidate=copy.deepcopy(candidate)
            if ds in events[sym]:
                # Do not execute stale raw-price plans across ex dates; await new signal.
                skips.append({'date':ds,'symbol':sym,'reason':'公司行動日取消舊買單'});continue
            if row.raw_open<=candidate['support']:
                skips.append({'date':ds,'symbol':sym,'reason':'開盤跌破或等於支撐，當日取消'});continue
            prior=data[sym].loc[data[sym].index<date].iloc[-1]
            if row.raw_high==row.raw_low and row.raw_close>=prior.raw_close*1.095:
                skips.append({'date':ds,'symbol':sym,'reason':'一價漲停，保守不假設買得到'});continue
            price=round_price(float(row.raw_open)*(1+cfg['slippage_bps']/10000),up=True)
            q,reason=size(candidate,price,equity,cash,positions,marks,meta,cfg)
            if not q:skips.append({'date':ds,'symbol':sym,'reason':reason});continue
            amount=q*price;fee=commission(amount,cfg);cash-=amount+fee
            positions[sym]=new_position(candidate,q,price,ds,cfg)
            ledger.append({'date':ds,'symbol':sym,'side':'BUY','quantity':q,'price':price,'amount':amount,'fee':fee,'tax':0,'weight':amount/equity,'stop':candidate['stop'],'support':candidate['support'],'resistance':candidate['resistance'],'reason':'前日觸及支撐區；不要求收盤確認、RSI 或報酬風險比' if entry_mode(cfg)=='support_touch' else '前日支撐反彈；開盤仍在支撐之上'})
        pending=[]
        for sym in meta:
            if date not in data[sym].index:continue
            row=data[sym].loc[date];marks[sym]=float(row.raw_close)
            if sym in positions:
                action,p,_=manage(positions[sym],row,cfg);p['last_managed_date']=ds;p['basis_date']=ds;positions[sym]=p
            elif eligible(meta[sym],ds):
                sig=signal(row,cfg)
                if sig:pending.append(dict(sig,symbol=sym,signal_date=ds))
        market_value=sum(p['quantity']*marks[s] for s,p in positions.items())
        equity=cash+market_value+sum(x['amount'] for x in receivables)
        curve.append({'date':ds,'equity':equity,'cash':cash,'receivables':sum(x['amount'] for x in receivables),'market_value':market_value,'positions':len(positions),'benchmark':bcash+bq*float(benchmark.loc[date].raw_close)+sum(x['amount'] for x in breceivables)})
        if cash<-.01:raise AssertionError('negative cash')
    frame=pd.DataFrame(curve)
    peaks=frame.equity.cummax().clip(lower=cfg['initial_cash'])
    dd=frame.equity/peaks-1
    liquidation_cost=sum(commission(p['quantity']*marks[s],cfg)+p['quantity']*marks[s]*cfg['sell_tax'] for s,p in positions.items())
    exposure=frame.market_value/frame.equity
    peak_index=exposure.idxmax()
    annual_rf=cfg.get('annual_risk_free_rate',0.0);periods=cfg.get('trading_days_per_year',252)
    summary={'entry_mode':entry_mode(cfg),'entry_rules':entry_rules(cfg),'start':str(sessions[0].date()),'end':str(sessions[-1].date()),'sessions':len(sessions),
             'initial_cash':cfg['initial_cash'],'end_equity':float(frame.equity.iloc[-1]),'return':float(frame.equity.iloc[-1]/cfg['initial_cash']-1),
             'sharpe_ratio':sharpe_ratio(frame.equity,cfg['initial_cash'],annual_rf,periods),
             'benchmark_sharpe_ratio':sharpe_ratio(frame.benchmark,cfg['initial_cash'],annual_rf,periods),
             'sharpe_assumptions':{'annualized':True,'annual_risk_free_rate':annual_rf,'trading_days_per_year':periods,'sample_std_ddof':1,'observations':len(frame),'first_day_vs_initial_cash':True,'include_flat_cash_days':True,'serial_correlation_adjusted':False},
             'max_drawdown':float(dd.min()),'benchmark_return':float(frame.benchmark.iloc[-1]/cfg['initial_cash']-1),
             'buys':sum(x['side']=='BUY' for x in ledger),'sells':sum(x['side']=='SELL' for x in ledger),'closed_trades':len(closed),
             'win_rate':sum(x['pnl']>0 for x in closed)/len(closed) if closed else None,
             'fees_and_tax':sum(x.get('fee',0)+x.get('tax',0) for x in ledger),'average_exposure':float((frame.market_value/frame.equity).mean()),
             'peak_close_exposure':float(exposure.max()),'peak_close_exposure_date':str(frame.loc[peak_index,'date']),'max_close_positions':int(frame.positions.max()),'end_exposure':float(exposure.iloc[-1]),
             'end_cash':cash,'end_receivables':sum(x['amount'] for x in receivables),'open_positions':positions,
             'hypothetical_exit_fees_tax':liquidation_cost,'warnings':sorted(set(warnings)),
             'limitations':['三個月探索回測；使用已檢視區間研究調整後規則，沒有獨立樣本外驗證','日線開盤加滑價為零股開盤後執行代理，無法重建逐筆及零股成交','公司行動採公開資料；現金股利列應收款至發放日；未提供發放日者維持應收','已知九月換股重建，非完整官方逐日歷史名單','暖機不足的股票不開新倉','期末按市價估值，未強制清倉；未扣個人股利所得稅、補充保費','Python 移植尚未逐檔與 TradingView 數值對帳']}
    return summary,frame,ledger,closed,skips


def save(result,destination):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    dest=Path(destination);dest.mkdir(parents=True,exist_ok=True)
    summary,curve,ledger,closed,skips=result
    (dest/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    for name,rows in [('trades',ledger),('closed_trades',closed),('skipped_orders',skips)]:
        (dest/(name+'.json')).write_text(json.dumps(rows,ensure_ascii=False,indent=2))
    curve.to_json(dest/'equity.json',orient='records',indent=2)
    fig,(ax,dd)=plt.subplots(2,1,figsize=(11,6),sharex=True,gridspec_kw={'height_ratios':[3,1]})
    dates=pd.to_datetime(curve.date)
    ax.plot(dates,curve.equity/summary['initial_cash']*100,label='Strategy, net of trading costs')
    ax.plot(dates,curve.benchmark/summary['initial_cash']*100,label='0050 buy & hold + dividends')
    ax.set_ylabel('Initial capital = 100');ax.legend();ax.grid(alpha=.2)
    dd.fill_between(dates,(curve.equity/curve.equity.cummax().clip(lower=summary['initial_cash'])-1)*100,0,alpha=.4)
    dd.set_ylabel('DD %');dd.grid(alpha=.2)
    fig.suptitle(f"TW50 strategy | {summary['start']} to {summary['end']} | daily execution proxy")
    fig.tight_layout();fig.savefig(dest/'equity.png',dpi=160);plt.close(fig)
    fmt_ratio=lambda value:'N/A（樣本不足或波動為零）' if value is None else f'{value:.2f}'
    assumptions=summary['sharpe_assumptions']
    rows=['# 三個月探索回測',f"期間：{summary['start']} ～ {summary['end']}，{summary['sessions']} 個交易日。",'',
          '| 項目 | 結果 |','|---|---:|',f"| 起始資金 | {summary['initial_cash']:,.0f} 元 |",f"| 期末淨值 | {summary['end_equity']:,.2f} 元 |",f"| 區間累積報酬（未年化） | {summary['return']:.2%} |",f"| 最大回撤 | {summary['max_drawdown']:.2%} |",f"| 策略年化 Sharpe Ratio | {fmt_ratio(summary['sharpe_ratio'])} |",f"| 0050 年化 Sharpe Ratio | {fmt_ratio(summary['benchmark_sharpe_ratio'])} |",f"| 0050 含股利區間報酬（未年化） | {summary['benchmark_return']:.2%} |",f"| 買進／賣出筆數 | {summary['buys']} / {summary['sells']} |",f"| 已平倉交易 | {summary['closed_trades']} |",f"| 手續費＋交易稅 | {summary['fees_and_tax']:,.2f} 元 |",f"| 平均資金投入 | {summary['average_exposure']:.2%} |",f"| 最高收盤持倉比例 | {summary['peak_close_exposure']:.2%}（{summary['peak_close_exposure_date']}） |",f"| 最多同時持有檔數（收盤） | {summary['max_close_positions']} |",f"| 期末持倉比例 | {summary['end_exposure']:.2%} |",f"| 期末現金 | {summary['end_cash']:,.2f} 元 |",'',
          '![績效曲線](equity.png)','', '這是固定規則的短期探索結果，無法據此認定有長期優勢。', '']
    rows += [f"Sharpe：每日淨值簡單報酬減每日無風險報酬，其算術平均／樣本標準差 × √{assumptions['trading_days_per_year']}。年無風險利率假設 {assumptions['annual_risk_free_rate']:.2%}（可修改，非市場利率報價），按複利換算每日利率。共 {assumptions['observations']} 筆日報酬，含首日相對起始資金的損益及空手日；淨值含費稅與未實現損益。", '', '只有三個月且交易少，年化值不代表一年實績；未調整報酬自相關。波動為零或少於2筆日報酬時為 N/A，JSON 為 null。', '', '[計算定義：William F. Sharpe, The Sharpe Ratio (1994)](https://web.stanford.edu/~wfsharpe/art/sr/sr.htm)', '']
    rows += ['- '+x for x in summary['limitations']+summary['warnings']]
    (dest/'report.md').write_text('\n'.join(rows))
