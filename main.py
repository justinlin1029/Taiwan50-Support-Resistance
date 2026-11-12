#!/usr/bin/env python3
"""Usage: python main.py --help. All defaults resolve relative to this file."""
import argparse
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
from inventory import transaction,write_json,reconcile,apply_fills,adopt
from strategy import entry_mode,entry_rules

ROOT=Path(__file__).resolve().parent


def validate_config(cfg):
    import math
    entry_mode(cfg)
    for k in ['risk_fraction','max_position_fraction','max_total_risk_fraction']:
        if not 0<cfg[k]<=1:raise ValueError(f'{k} 必須在 0 到 1 之間')
    if cfg.get('max_sector_fraction') is not None and not 0<cfg['max_sector_fraction']<=1:
        raise ValueError('max_sector_fraction 必須為 null（不限產業）或大於 0 且不超過 1')
    for k in ['initial_cash','min_rr','stop_atr','trail_atr']:
        if not math.isfinite(cfg[k]) or cfg[k]<=0:raise ValueError(f'{k} 必須大於零')
    for k in ['commission_rate','minimum_commission','sell_tax','slippage_bps']:
        if not math.isfinite(cfg[k]) or cfg[k]<0:raise ValueError(f'{k} 不可為負數')
    from metrics import sharpe_ratio
    sharpe_ratio([],cfg['initial_cash'],cfg.get('annual_risk_free_rate',0.0),cfg.get('trading_days_per_year',252))
    if cfg['lot_size'] not in [1,1000]:raise ValueError('lot_size 僅支援 1（單筆零股最多999股）或 1000（整張）')
    if cfg['swing_length']<2 or cfg['profile_rows']<2 or cfg['profile_lookback']<40:raise ValueError('指標參數過小')


def main():
    parser=argparse.ArgumentParser(description='0050 成分股：日線訊號、庫存管理、三個月探索回測（不下單）')
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--config',type=Path,help='覆寫設定檔，預設 root/config.json')
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('download',help='免費資料下載；預設含約三年暖機資料')
    p.add_argument('--end',default=datetime.now(ZoneInfo('Asia/Taipei')).date().isoformat());p.add_argument('--start');p.add_argument('--refresh-universe',action='store_true')
    sub.add_parser('prepare',help='驗證及處理已下載資料')
    p=sub.add_parser('backtest',help='預設只計最近三個月績效')
    p.add_argument('--end');p.add_argument('--start');p.add_argument('--output',type=Path);p.add_argument('--slippage-bps',type=float);p.add_argument('--allow-current-universe',action='store_true')
    p=sub.add_parser('plan',help='依真實庫存產生下一交易日條件清單')
    p.add_argument('--as-of',required=True)
    p=sub.add_parser('quotes',help='依開盤及當下報價重算可買數量；不改庫存')
    p.add_argument('--plan',type=Path,required=True);p.add_argument('--file',type=Path,required=True)
    p=sub.add_parser('snapshot',help='匯入完整真實持倉及現金，保留歷史備份')
    p.add_argument('--file',type=Path,required=True)
    p=sub.add_parser('fills',help='匯入實際成交；fill_id 防止重複記帳')
    p.add_argument('--file',type=Path,required=True);p.add_argument('--plan',type=Path)
    p=sub.add_parser('adopt',help='替既有持股明確設定支撐、壓力、停損')
    p.add_argument('--file',type=Path,required=True)
    sub.add_parser('account',help='顯示真實帳戶')
    args=parser.parse_args();root=args.root.resolve();cfg=json.loads((args.config or root/'config.json').read_text());u=json.loads((root/'data/universe.json').read_text())
    validate_config(cfg)
    if args.command=='download':
        from market_data import download,prepare,refresh_universe
        if args.refresh_universe:refresh_universe(root)
        start=args.start or str((pd.Timestamp(args.end)-pd.DateOffset(years=3,months=3)).date())
        download(root,start,args.end);print(prepare(root));return
    if args.command=='prepare':
        from market_data import prepare
        print(json.dumps(prepare(root),ensure_ascii=False,indent=2));return
    if args.command in ['snapshot','fills','adopt']:
        payload=json.loads(args.file.read_text())
        if payload.get('example_only'):raise ValueError('這是範例檔；填入真實資料並移除 example_only 後才可記帳')
        with transaction(root) as account:
            if args.command=='snapshot':print(reconcile(account,payload))
            elif args.command=='fills':
                plan=json.loads(args.plan.read_text()) if args.plan else None
                if plan:
                    for fill in payload['fills']:
                        prepared=root/'data/prepared'/f"{fill['symbol']}.json"
                        if prepared.exists():
                            events=json.loads(prepared.read_text())['events']
                            if any(plan['as_of']<d<=fill['date'] for d in events):fill['manual']=True
                apply_fills(account,payload['fills'],cfg,plan)
            else:adopt(account,payload,cfg)
        print('庫存已更新，前一版保存在 inventory_history/');return
    if args.command=='account':print((root/'account.json').read_text());return
    if args.command=='quotes':
        from planner import opening_check
        plan=json.loads(args.plan.read_text());quotes=json.loads(args.file.read_text());account=json.loads((root/'account.json').read_text())
        if quotes.get('example_only'):raise ValueError('報價是格式範例；請改成當日真實報價並移除 example_only')
        out=opening_check(plan,quotes,account,u,cfg);dest=root/'reports'/f"opening_{quotes['trade_date']}.json";write_json(dest,out);print(json.dumps(out,ensure_ascii=False,indent=2));return
    from market_data import load
    data,events=load(root,cfg)
    if args.command=='backtest':
        from backtest import run,save
        end=args.end or str(data['0050'].index[-1].date())
        start=args.start or str((pd.Timestamp(end)+pd.Timedelta(days=1)-pd.DateOffset(months=3)).date())
        if u.get('mode')=='current_snapshot' and not args.allow_current_universe:raise ValueError('只有最新成分股快照；如接受存活者偏誤，明確加入 --allow-current-universe')
        if u.get('history_start') and start<u['history_start']:raise ValueError('回測起日早於已重建成分股區間')
        if args.slippage_bps is not None:cfg['slippage_bps']=args.slippage_bps
        result=run(data,events,u,cfg,start,end)
        if u.get('mode')=='current_snapshot':result[0]['limitations'].append('使用最新成分股回測，有存活者偏誤')
        crosscheck=root/'data/close_crosscheck.json'
        if crosscheck.exists():
            cross=json.loads(crosscheck.read_text())
            relevant=[x for x in cross.get('mismatches',[]) if start<=x['date']<=end]
            if relevant:result[0]['limitations'].append(f'雙來源收盤比對有 {len(relevant)} 筆差異超過0.1%；保留FinMind原始價，詳見data/close_crosscheck.json')
        destination=args.output or root/'reports'/f'backtest_{end}'
        save(result,destination);write_json(destination/'config_used.json',cfg)
        from diagnostics import screening,audit_candidates
        diagnostic=screening(data,u,cfg,start,end)
        write_json(destination/'screening_diagnostics.json',diagnostic)
        audit=audit_candidates(data,u,cfg,result)
        write_json(destination/'candidate_audit.json',audit)
        with (destination/'report.md').open('a') as f:
            f.write(f"\n\n本次規則：{entry_rules(cfg)}。\n")
            sector='不限制、不使用產業分類' if cfg.get('max_sector_fraction') is None else f"上限 {cfg['max_sector_fraction']:.0%}"
            f.write(f"\n配置：單股上限 {cfg['max_position_fraction']:.0%}；每筆初始風險 {cfg['risk_fraction']:.0%}；整體初始風險 {cfg['max_total_risk_fraction']:.0%}；產業：{sector}；不加碼。\n")
            f.write('\n\n## 訊號篩選統計\n\n')
            labels={'eligible_stock_days':'股票池內的股日','warmup_ready':'暖機足夠','has_support_resistance':'同時有支撐與壓力','touch_support':'進入支撐區','close_back_above':'收盤站回支撐且未追高','rsi_at_least_30':'RSI ≥ 30','gross_rr_at_close_at_least_threshold':f"收盤價毛報酬風險比 ≥ {cfg['min_rr']:g}",'conditional_candidates':'存在可掛條件的進場候選'}
            for k,v in diagnostic['stages'].items():f.write(f'- {labels[k]}：{v}\n')
            f.write('\n期末最後一天的候選只供隔日觀察，不假設已成交。完整原因見 screening_diagnostics.json 與 skipped_orders.json。\n')
            f.write(f"\n## 候選去向對帳\n\n包含回測首日前一交易日的訊號，共 {audit['total']} 筆股票日候選（{audit['first_signal_date']}～{audit['last_signal_date']}）。\n\n")
            audit_labels={'already_held':'已持有，不加碼','bought':'實際模擬買進','gap_cancel':'隔日開盤跌破／等於支撐，取消','outside_period':'下一交易日在回測範圍外','other_cancel':'其他原因取消'}
            for key,count in audit['counts'].items():f.write(f"- {audit_labels[key]}：{count}\n")
            f.write("\n每筆日期與原因見 candidate_audit.json；買進筆數與已平倉交易數是不同統計。\n")

        print(json.dumps({k:v for k,v in result[0].items() if k!='open_positions'},ensure_ascii=False,indent=2));print(destination)
    elif args.command=='plan':
        from planner import make_plan,markdown
        with transaction(root) as account:
            plan,new=make_plan(data,events,u,account,cfg,args.as_of)
            write_json(root/'reports'/f'plan_{args.as_of}.json',plan)
            (root/'reports'/f'plan_{args.as_of}.md').write_text(markdown(plan))
            account.clear();account.update(new)
        print(markdown(plan))


if __name__=='__main__':
    try:main()
    except (ValueError,KeyError,FileNotFoundError) as e:
        raise SystemExit(f'ERROR: {e}')
