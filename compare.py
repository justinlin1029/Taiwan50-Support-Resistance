"""Reproduce the requested paired entry-rule comparison without changing accounts."""
import argparse
import json
import subprocess
import sys
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parent


def read(path):
    return json.loads(path.read_text())


def main():
    parser=argparse.ArgumentParser(description='現行收盤確認 vs 觸及支撐即列隔日候選')
    parser.add_argument('--start',required=True)
    parser.add_argument('--end',required=True)
    parser.add_argument('--config',type=Path,default=ROOT/'config.json')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    dest=(args.output or ROOT/'reports'/f'comparison_{args.end}').resolve()
    dest.mkdir(parents=True,exist_ok=True)
    cfg=read(args.config)
    account_before=(ROOT/'account.json').read_bytes()
    summaries={};diagnostics={};curves={};trades={};configs={}
    for name,mode in [('current','close_confirmation'),('touch','support_touch')]:
        settings={**cfg,'entry_mode':mode}
        config_path=dest/f'config_{name}.json'
        config_path.write_text(json.dumps(settings,ensure_ascii=False,indent=2)+'\n')
        configs[name]=settings
        command=[sys.executable,str(ROOT/'main.py'),'--config',str(config_path),'backtest',
                 '--start',args.start,'--end',args.end,'--output',str(dest/name)]
        result=subprocess.run(command,text=True,capture_output=True)
        if result.returncode:
            raise RuntimeError(result.stdout+result.stderr)
        summaries[name]=read(dest/name/'summary.json')
        diagnostics[name]=read(dest/name/'screening_diagnostics.json')
        curves[name]=pd.read_json(dest/name/'equity.json')
        trades[name]=read(dest/name/'trades.json')
        print(f'{name}: return={summaries[name]["return"]:.2%}, buys={summaries[name]["buys"]}, Sharpe={summaries[name]["sharpe_ratio"]}',flush=True)
    assert {k:v for k,v in configs['current'].items() if k!='entry_mode'}=={k:v for k,v in configs['touch'].items() if k!='entry_mode'}
    assert (ROOT/'account.json').read_bytes()==account_before,'真實庫存不應改變'
    assert curves['current'].date.equals(curves['touch'].date)
    assert curves['current'].benchmark.equals(curves['touch'].benchmark)
    buys={k:{(t['symbol'],t['date']):t for t in v if t['side']=='BUY'} for k,v in trades.items()}
    added=[buys['touch'][key] for key in sorted(buys['touch'].keys()-buys['current'].keys(),key=lambda k:(k[1],k[0]))]
    removed=[buys['current'][key] for key in sorted(buys['current'].keys()-buys['touch'].keys(),key=lambda k:(k[1],k[0]))]
    payload={'period':[args.start,args.end],'current':summaries['current'],'touch':summaries['touch'],
             'additional_entry_dates':added,'missing_entry_dates':removed,
             'difference':'取消收盤確認、RSI 及報酬風險比門檻；其餘設定相同；均隔日開盤代理成交',
             'note':'entry_mode 為唯一設定差異；touch 模式不使用 config 中保留的 min_rr 值。'}
    (dest/'comparison.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
    fmt_pct=lambda x:f'{x:.2%}'
    fmt_num=lambda x:'N/A' if x is None else f'{x:.2f}'
    rows=['# 進場規則比較',f'期間：{args.start}～{args.end}；起始資金 {cfg["initial_cash"]:,.0f} 元。','',
          '| 規則 | 現行版 | 觸及支撐版 |','|---|---|---|',
          '| 最低價在 S±0.5ATR | 必須 | 必須 |',
          '| S < 收盤 ≤ S+ATR | 必須 | 不要求 |',
          '| RSI ≥30 | 必須 | 不要求 |',
          f'| 扣成本報酬風險比 ≥{cfg["min_rr"]:g} | 必須 | 不要求 |',
          '| 進場時間 | 下一交易日 | 下一交易日 |',
          '| 開盤跌破／等於支撐 | 取消 | 取消 |','',
          '兩版均須先存在可用支撐與壓力；沿用原指標、停損、出場與部位限制。觸及版取消報酬風險比推算的最高買價，可能在隔日買得較貴，甚至高於原壓力；仍須高於支撐且通過資金與停損風險額度。多檔候選仍依原收盤報酬風險比排序，但觸及版不以此作門檻。',
          '', '| 結果 | 現行版 | 觸及支撐版 |','|---|---:|---:|']
    for label,key,fmt in [('買進次數','buys',str),('賣出次數','sells',str),('已平倉交易','closed_trades',str),
                          ('帳戶報酬','return',fmt_pct),('最大回撤','max_drawdown',fmt_pct),
                          ('年化 Sharpe','sharpe_ratio',fmt_num),('已平倉勝率','win_rate',lambda x:'N/A' if x is None else fmt_pct(x)),
                          ('平均資金投入','average_exposure',fmt_pct),('費用與交易稅','fees_and_tax',lambda x:f'{x:,.2f} 元'),
                          ('期末淨值','end_equity',lambda x:f'{x:,.2f} 元')]:
        rows.append(f'| {label} | {fmt(summaries["current"][key])} | {fmt(summaries["touch"][key])} |')
    rows += [f'| 期末未平倉檔數 | {len(summaries["current"]["open_positions"])} | {len(summaries["touch"]["open_positions"])} |',
             f'| 條件候選股票日數 | {diagnostics["current"]["stages"]["conditional_candidates"]} | {diagnostics["touch"]["stages"]["conditional_candidates"]} |','',
             f'觸及版相對現行版的報酬差為 {(summaries["touch"]["return"]-summaries["current"]["return"])*100:+.2f} 個百分點，期末淨值差 {summaries["touch"]["end_equity"]-summaries["current"]["end_equity"]:+,.2f} 元。更高投入與不同持有期間也會影響損益，不能單以總報酬或四筆已平倉的勝率判斷優劣。', '',
             f'共同0050含股利基準：{summaries["current"]["benchmark_return"]:.2%}，年化 Sharpe {fmt_num(summaries["current"]["benchmark_sharpe_ratio"])}。',
             f'成本假設：手續費 {cfg["commission_rate"]:.4%}、最低 {cfg["minimum_commission"]} 元、賣出稅 {cfg["sell_tax"]:.2%}、單邊滑價 {cfg["slippage_bps"]:g} 基點。',
             f'Sharpe 採每日淨值報酬，無風險年利率 {cfg.get("annual_risk_free_rate",0):.2%}、年交易日數 {cfg.get("trading_days_per_year",252)}；含空手日、首日及未實現損益，未做自相關修正。',
             '', '![兩版淨值與回撤](comparison.png)','', '## 觸及版新增的進場日期','',
             '| 日期 | 股票 | 股數 | 模擬成交價 | 帳戶比例 |','|---|---|---:|---:|---:|']
    for t in added:
        rows.append(f'| {t["date"]} | {t["symbol"]} | {t["quantity"]} | {t["price"]:.2f} | {t["weight"]:.2%} |')
    if not added:rows.append('| — | 沒有新增進場日期 | — | — | — |')
    rows += ['',f'現行版有、觸及版沒有的進場日期：{len(removed)} 筆（如有，詳見 comparison.json）。新增日期不等同新增獨立交易機會；更早進場可能取代原本後續進場。',
             '', '## 解讀限制','',
             '- 這次同時取消三項篩選，不能把績效差異單獨歸因於收盤確認、RSI 或報酬風險比。',
             '- 僅比較已看過的同一段三個月，並非樣本外測試；少數交易與少量投入可能使年化 Sharpe 不穩定。',
             '- 觸及指「整天最低價在支撐區內」；當天曾經穿越但最低價跌到區域下方，仍不列入。兩版皆非盤中觸價成交。',
             '- 日線開盤加滑價是零股執行代理；當日收盤訊號不會用來回填當日成交。',
             '- 期末按市價估值、未強制賣出；未平倉交易不納入勝率。資料與指標移植限制見各版報告。',
             '- 此比較不切換日常策略，也不改動真實庫存。',
             '', '[現行版明細](current/report.md) · [觸及版明細](touch/report.md)']
    rows += ['', '## 期末模擬庫存', '', '| 版本 | 股票 | 股數 | 進場日 | 後續狀態 |', '|---|---|---:|---|---|']
    for name,label in [('current','現行版'),('touch','觸及版')]:
        for sym,p in summaries[name]['open_positions'].items():
            rows.append(f'| {label} | {sym} | {p["quantity"]:g} | {p["entry_date"]} | {p.get("pending_exit") or "依原規則持有"} |')
    rows += ['', '以上均為模擬部位，並非真實帳戶。待賣訊號在期末之後才執行，本回測不補入區間外成交。']
    (dest/'comparison.md').write_text('\n'.join(rows)+'\n')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,(ax,dd)=plt.subplots(2,1,figsize=(11,6),sharex=True,gridspec_kw={'height_ratios':[3,1]})
    for key,label in [('current','Current: close + RSI + net RR'),('touch','Touch: no close / RSI / RR filters')]:
        frame=curves[key];dates=pd.to_datetime(frame.date)
        ax.plot(dates,frame.equity/cfg['initial_cash']*100,label=label)
        dd.plot(dates,(frame.equity/frame.equity.cummax().clip(lower=cfg['initial_cash'])-1)*100,label=label)
    ax.plot(dates,curves['current'].benchmark/cfg['initial_cash']*100,label='0050 benchmark',alpha=.6)
    ax.set_ylabel('Initial capital = 100');dd.set_ylabel('Drawdown %')
    ax.legend();ax.grid(alpha=.2);dd.grid(alpha=.2)
    fig.suptitle(f'Entry rule comparison | {args.start} to {args.end} | next-session proxy')
    fig.tight_layout();fig.savefig(dest/'comparison.png',dpi=160);plt.close(fig)
    print(dest/'comparison.md')


if __name__=='__main__':
    main()
