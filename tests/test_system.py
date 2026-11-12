import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from indicators import rma,profile,compute
from strategy import signal,manage,size,new_position,adjust_position
from inventory import apply_fills,reconcile,transaction,write_json
from planner import opening_check,make_plan,markdown
from backtest import run
from metrics import sharpe_ratio
from diagnostics import audit_candidates

ROOT=Path(__file__).resolve().parents[1]
# Existing close-confirmation regressions stay explicit when the live default changes.
CFG={**json.loads((ROOT/'config_touch_10pct.example.json').read_text()),'entry_mode':'close_confirmation'}


def cand(symbol='2330'):
    return {'symbol':symbol,'name':'test','sector':'Semi','support':100.,'resistance':130.,'stop':95.,'reference_price':102.,'max_buy_price':106.,'atr':5.,'avg_volume':1e7,'rr':4.,'rsi':40.,'signal_date':'2026-10-16'}


def account():return {'as_of':'2026-10-16','cash':260000.,'positions':{},'processed_fill_ids':[]}


def row(**kwargs):
    d={'factor':1.,'history_count':700,'support':100.,'resistance':130.,'atr_prev':5.,'atr':5.,'raw_close':102.,'raw_low':99.,'raw_high':105.,'raw_open':101.,'rsi':40.,'rsi_prev':35.,'volume_mean20':1e6,'volume_factor':1.,'raw_volume':1e6,'support_score':50.}
    d.update(kwargs);return pd.Series(d)


class PerformanceMetrics(unittest.TestCase):
    def test_first_day_and_sample_standard_deviation(self):
        # +1%, +3%: mean 2%, sample variance .0002; first day is essential.
        self.assertAlmostEqual(sharpe_ratio([101,104.03],100),.02/np.sqrt(.0002)*np.sqrt(252))
        self.assertAlmostEqual(sharpe_ratio([110,99],100),0.)

    def test_risk_free_conversion_and_flat_cash_days(self):
        expected=(.02-((1.04)**(1/252)-1))/np.sqrt(.0002)*np.sqrt(252)
        self.assertAlmostEqual(sharpe_ratio([101,104.03],100,.04),expected)
        # Daily returns 0%, 0%, 1% must not be reduced to trade-only returns.
        self.assertAlmostEqual(sharpe_ratio([100,100,101],100),np.sqrt(84))

    def test_undefined_and_invalid_metrics(self):
        for values in [[],[101],[100,100,100],[110,121,133.1]]:
            self.assertIsNone(sharpe_ratio(values,100))
        for values in [[100,np.nan],[100,0],[100,np.inf]]:
            with self.assertRaises(ValueError):sharpe_ratio(values,100)
        for rf in [-1,float('nan')]:
            with self.assertRaises(ValueError):sharpe_ratio([101,102],100,rf)
        with self.assertRaises(ValueError):sharpe_ratio([101,102],100,0,0)


class Indicators(unittest.TestCase):
    def test_wilder_seed(self):
        v=rma([np.nan,1,2,3,6],3)
        self.assertTrue(np.isnan(v[2]));self.assertEqual(v[3],2);self.assertAlmostEqual(v[4],10/3)

    def test_volume_conservation(self):
        _,_,total,buy=profile(np.array([12.,11.]),np.array([10.,11.]),np.array([11.,11.]),np.array([100.,50.]),2,.01)
        self.assertAlmostEqual(total.sum(),150);self.assertAlmostEqual(buy.sum(),75)

    def test_prefix_invariance(self):
        rng=np.random.default_rng(8);n=220;c=100+np.cumsum(rng.normal(0,1,n))
        f=pd.DataFrame({'open':c-.2,'high':c+1,'low':c-1,'close':c,'volume':rng.integers(1000,5000,n),'factor':1.},index=pd.bdate_range('2025-01-01',periods=n))
        full=compute(f,CFG);prefix=compute(f.iloc[:160],CFG)
        pd.testing.assert_frame_equal(full.iloc[:160],prefix)

    def test_delayed_pivot(self):
        # A crash added to future observations cannot alter earlier output.
        n=100;c=np.full(n,100.);c[80]=80
        f=pd.DataFrame({'open':c,'high':c+1,'low':c-1,'close':c,'volume':10000.,'factor':1.})
        a=compute(f.iloc[:83],CFG);b=compute(f,CFG)
        pd.testing.assert_frame_equal(a,b.iloc[:83])


class Trading(unittest.TestCase):
    def test_entry_requires_rebound(self):
        self.assertIsNotNone(signal(row(),CFG))
        self.assertIsNone(signal(row(raw_close=99),CFG))
        self.assertIsNone(signal(row(rsi=29),CFG))
        self.assertIsNotNone(signal(row(rsi_prev=45),CFG))
        self.assertIsNotNone(signal(row(rsi=30,rsi_prev=40),CFG))
        self.assertIsNotNone(signal(row(rsi_prev=np.nan),CFG))
        self.assertIsNone(signal(row(rsi=np.nan),CFG))
        self.assertIsNone(signal(row(raw_close=100),CFG))
        self.assertIsNotNone(signal(row(raw_close=105),CFG))
        self.assertIsNone(signal(row(raw_close=105.01),CFG))

    def test_net_rr_1p2_includes_costs(self):
        c=cand();c['resistance']=112.
        meta={'2330':{'sector':'Semi'}}
        q,_=size(c,102,260000,260000,{}, {},meta,CFG)
        self.assertGreater(q,0)
        strict={**CFG,'min_rr':2.}
        q,_=size(c,102,260000,260000,{}, {},meta,strict)
        self.assertEqual(q,0)
        c['resistance']=111. # Gross RR > 1.2, but net RR < 1.2.
        q,reason=size(c,102,260000,260000,{}, {},meta,CFG)
        self.assertEqual(q,0)
        self.assertIn('成本',reason)

    def test_touch_mode_removes_only_requested_entry_filters(self):
        touch={**CFG,'entry_mode':'support_touch'}
        for close,rsi in [(99,29),(110,20),(102,np.nan)]:
            self.assertIsNone(signal(row(raw_close=close,rsi=rsi),CFG))
            c=signal(row(raw_close=close,rsi=rsi),touch)
            self.assertIsNotNone(c);self.assertIsNone(c['max_buy_price'])
        self.assertIsNone(signal(row(raw_low=97),touch))
        self.assertIsNone(signal(row(resistance=np.nan),touch))
        self.assertIsNone(signal(row(history_count=10),touch))
        c={**signal(row(resistance=103),touch),'symbol':'2330'}
        meta={'2330':{'sector':'Semi'}}
        # Price above resistance is allowed without an RR filter, but risk caps apply.
        q,_=size(c,104,260000,260000,{}, {},meta,touch)
        self.assertGreater(q,0);self.assertLessEqual(q*104,26000)
        q,_=size(c,100,260000,260000,{}, {},meta,touch);self.assertEqual(q,0)
        q,_=size(c,104,260000,0,{}, {},meta,touch);self.assertEqual(q,0)

    def test_next_open_break_cancels_even_if_recovers(self):
        plan={'as_of':'2026-10-16','account_as_of':'2026-10-16','candidates':[cand()]}
        q={'signal_date':'2026-10-16','trade_date':'2026-10-19','is_next_trading_session':True,'quotes':{'2330':{'open':99,'price':102}}}
        result=opening_check(plan,q,account(),{'constituents':[{'symbol':'2330','sector':'Semi'}]},CFG)
        self.assertEqual(result['orders'][0]['quantity'],0)
        self.assertIn('開盤',result['orders'][0]['reason'])

    def test_equal_support_reject(self):
        q,_=size(cand(),100,260000,260000,{}, {},{'2330':{'sector':'Semi'}},CFG)
        self.assertEqual(q,0)

    def test_price_too_high(self):
        q,_=size(cand(),107,260000,260000,{}, {},{'2330':{'sector':'Semi'}},CFG);self.assertEqual(q,0)

    def test_sector_and_risk_caps(self):
        meta={'2330':{'sector':'Semi'},'2303':{'sector':'Semi'}}
        existing=new_position(cand('2303'),750,102,'2026-10-15',CFG)
        existing['initial_risk']=100
        q,_=size(cand(),102,260000,180000,{'2303':existing},{'2303':102},meta,CFG)
        self.assertLessEqual(750*102+q*102,260000*.3)
        existing['initial_risk']=260000*.03
        q,_=size(cand(),102,260000,180000,{'2303':existing},{'2303':102},meta,CFG);self.assertEqual(q,0)

    def test_disabled_sector_is_independent_of_labels_and_missing_metadata(self):
        cfg=json.loads((ROOT/'config.example.json').read_text())
        from main import validate_config
        validate_config(cfg)
        self.assertIsNone(cfg['max_sector_fraction'])
        self.assertEqual(cfg['max_total_risk_fraction'],.15)
        existing=new_position(cand('2303'),800,102,'2026-10-15',cfg)
        # Existing holding alone already exceeds the former 30% sector cap.
        positions={'2303':existing};marks={'2303':102}
        quantities=[]
        for metadata in [{},{'2330':{},'2303':{}},
                         {'2330':{'sector':'Semi'},'2303':{'sector':'Semi'}},
                         {'2330':{'sector':'A'},'2303':{'sector':'B'}}]:
            q,_=size(cand(),102,260000,178400,positions,marks,metadata,cfg)
            quantities.append(q)
        self.assertEqual(quantities,[764]*4)
        self.assertLessEqual(quantities[0]*102,260000*.3)

    def test_live_risk_limits_include_costs_and_remaining_portfolio_budget(self):
        cfg=json.loads((ROOT/'config.example.json').read_text())
        c={**cand(),'stop':75.} # Enough distance for risk, rather than capital, to bind.
        q,_=size(c,102,260000,260000,{}, {},{},cfg)
        risk=new_position(c,q,102,'2026-10-19',cfg)['initial_risk']
        self.assertGreater(risk,7800) # Former 3% cap no longer blocks this trade.
        self.assertLessEqual(risk,13000)
        self.assertGreater(new_position(c,q+1,102,'2026-10-19',cfg)['initial_risk'],13000)
        existing={'2303':{'quantity':1,'mode':'range','initial_risk':38000}}
        q,_=size(c,102,260000,250000,existing,{}, {},cfg)
        self.assertGreater(q,0)
        self.assertLessEqual(new_position(c,q,102,'2026-10-19',cfg)['initial_risk'],1000)
        existing['2303']['initial_risk']=39000
        q,reason=size(c,102,260000,250000,existing,{}, {},cfg)
        self.assertEqual(q,0);self.assertNotIn('產業',reason)

    def test_costs_and_cash(self):
        q,_=size(cand(),102,260000,200,{}, {},{'2330':{'sector':'Semi'}},CFG)
        self.assertEqual(q,0) # Tiny trade fails net RR after minimum commissions.

    def test_stop_priority(self):
        p=new_position(cand(),10,102,'2026-10-15',CFG)
        action,_,reason=manage(p,row(raw_close=94,raw_high=140),CFG)
        self.assertEqual(action,'SELL');self.assertIn('停損',reason)

    def test_trail_does_not_decrease(self):
        p=new_position(cand(),10,102,'2026-10-15',CFG);p.update(mode='trend',stop=120,highest_close=140)
        a,new,_=manage(p,row(raw_close=135,atr=15),CFG)
        self.assertEqual(a,'HOLD');self.assertEqual(new['stop'],120)

    def test_breakout_before_resistance_exit(self):
        p=new_position(cand(),10,102,'2026-10-15',CFG)
        a,new,_=manage(p,row(raw_close=134,raw_high=135),CFG)
        self.assertEqual(a,'HOLD');self.assertEqual(new['mode'],'trend');self.assertEqual(new['stop'],124)

    def test_dividend_adjustment(self):
        p=new_position(cand(),100,102,'2026-10-15',CFG)
        q=adjust_position(p,{'split':2,'factor':2})
        self.assertEqual(q['quantity'],200);self.assertEqual(q['stop'],47.5);self.assertEqual(q['initial_risk'],p['initial_risk'])

    def test_zero_capital(self):
        q,_=size(cand(),102,0,0,{}, {},{'2330':{'sector':'Semi'}},CFG);self.assertEqual(q,0)


class Inventory(unittest.TestCase):
    def test_stale_plan_fill_becomes_unmanaged(self):
        a=account();plan={'as_of':'2026-10-16','candidates':[cand()]}
        apply_fills(a,[{'fill_id':'late','date':'2026-11-02','symbol':'2330','side':'BUY','quantity':10,'price':102}],CFG,plan)
        self.assertEqual(a['positions']['2330']['mode'],'unmanaged')

    def test_fill_idempotent_and_partial_exit(self):
        a=account();plan={'as_of':'2026-10-16','candidates':[cand()]}
        buy={'fill_id':'B1','date':'2026-10-19','symbol':'2330','side':'BUY','quantity':100,'price':102,'fee':20,'tax':0}
        apply_fills(a,[buy],CFG,plan);cash=a['cash'];risk=a['positions']['2330']['initial_risk']
        apply_fills(a,[buy],CFG,plan);self.assertEqual(a['cash'],cash)
        sell={'fill_id':'S1','date':'2026-10-20','symbol':'2330','side':'SELL','quantity':40,'price':110,'fee':20,'tax':13.2}
        apply_fills(a,[sell],CFG)
        self.assertEqual(a['positions']['2330']['quantity'],60);self.assertAlmostEqual(a['positions']['2330']['initial_risk'],risk*.6)
        self.assertAlmostEqual(a['cash'],cash+4400-20-13.2)

    def test_touch_fill_accepts_no_maximum_price(self):
        a=account();c=cand();c['max_buy_price']=None
        plan={'as_of':'2026-10-16','candidates':[c]}
        apply_fills(a,[{'fill_id':'touch','date':'2026-10-19','symbol':'2330','side':'BUY','quantity':100,'price':102,'fee':20,'tax':0}],{**CFG,'entry_mode':'support_touch'},plan)
        self.assertEqual(a['positions']['2330']['mode'],'range')
        self.assertEqual(a['positions']['2330']['quantity'],100)

    def test_oversell_rollback(self):
        a=account();original=copy.deepcopy(a)
        with self.assertRaises(ValueError):apply_fills(a,[{'fill_id':'x','date':'2026-10-19','symbol':'2330','side':'SELL','quantity':10,'price':100}],CFG)
        self.assertEqual(a,original)

    def test_unmanaged_manual_position(self):
        a=account();reconcile(a,{'as_of':'2026-10-19','complete_snapshot':True,'cash':200000,'positions':[{'symbol':'2330','quantity':100,'avg_cost':100}]})
        self.assertEqual(a['positions']['2330']['mode'],'unmanaged')
        with self.assertRaises(ValueError):reconcile(a,{'as_of':'2026-10-20','cash':200000,'positions':[]})

    def test_transaction_failure_preserves_file(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);write_json(root/'account.json',account())
            with self.assertRaises(ValueError):
                with transaction(root) as a:a['cash']=-1
            self.assertEqual(json.loads((root/'account.json').read_text())['cash'],260000)


class PortfolioBacktest(unittest.TestCase):
    def fixture(self,opening=102):
        idx=pd.to_datetime(['2026-10-15','2026-10-16','2026-10-19'])
        f=pd.DataFrame([row(),row(raw_open=opening,raw_close=94,raw_low=93),row(raw_open=90,raw_close=91,raw_low=89,raw_high=92)],index=idx)
        return {'0050':f.copy(),'2330':f},{'0050':{},'2330':{}},{'constituents':[{'symbol':'2330','sector':'Semi'}]}

    def test_signal_audit_distinguishes_held_filled_and_gap_cancel(self):
        d,e,u=self.fixture();touch={**CFG,'entry_mode':'support_touch'}
        # Remain above stop, producing a second signal while already held.
        d['2330'].loc[pd.Timestamp('2026-10-16'),['raw_close','raw_low']]=[102,99]
        result=run(d,e,u,touch,'2026-10-16','2026-10-19')
        audit=audit_candidates(d,u,touch,result)
        self.assertEqual(audit['counts'],{'bought':1,'already_held':1})
        self.assertEqual(audit['total'],2)
        d,e,u=self.fixture(99)
        result=run(d,e,u,touch,'2026-10-16','2026-10-19')
        audit=audit_candidates(d,u,touch,result)
        self.assertEqual(audit['counts'],{'gap_cancel':1})

    def test_touch_mode_waits_for_next_session_and_keeps_gap_cancel(self):
        d,e,u=self.fixture()
        d['2330'].loc[pd.Timestamp('2026-10-15'),['raw_close','rsi']]=[99,20]
        baseline=run(d,e,u,CFG,'2026-10-16','2026-10-19')
        touch={**CFG,'entry_mode':'support_touch'}
        result=run(d,e,u,touch,'2026-10-16','2026-10-19')
        self.assertEqual(baseline[0]['buys'],0)
        self.assertEqual(result[2][0]['date'],'2026-10-16')
        self.assertEqual(result[0]['buys'],1)
        d['2330'].loc[pd.Timestamp('2026-10-16'),'raw_open']=99
        result=run(d,e,u,touch,'2026-10-16','2026-10-19')
        self.assertEqual(result[0]['buys'],0)

    def test_trade_lifecycle_gap_stop_actual_open(self):
        d,e,u=self.fixture();result=run(d,e,u,CFG,'2026-10-16','2026-10-19')
        trades=[x for x in result[2] if x['side'] in ['BUY','SELL']]
        self.assertEqual([x['side'] for x in trades],['BUY','SELL'])
        self.assertLess(trades[1]['price'],95) # Cannot fill at the stop after gap.
        self.assertEqual(trades[0]['date'],'2026-10-16')
        self.assertLess(result[0]['return'],0)

    def test_gap_below_support_no_entry(self):
        d,e,u=self.fixture(99);result=run(d,e,u,CFG,'2026-10-16','2026-10-19')
        self.assertEqual(result[0]['buys'],0)
        self.assertTrue(any('開盤' in x['reason'] for x in result[4]))

    def test_dividend_receivable_not_double_counted(self):
        d,e,u=self.fixture();d['2330'].loc[pd.Timestamp('2026-10-16'),'raw_close']=103
        e['2330']['2026-10-19']={'cash':2.,'split':1.,'factor':103/101,'pay_date':'2026-11-01','unsupported':False,'bonus':False}
        result=run(d,e,u,CFG,'2026-10-16','2026-10-19')
        q=[x for x in result[2] if x['side']=='BUY'][0]['quantity']
        self.assertEqual(result[0]['end_receivables'],q*2)

    def test_pending_stop_survives_suspension(self):
        d,e,u=self.fixture();d['0050'].loc[pd.Timestamp('2026-10-20')]=row(raw_open=90,raw_close=91)
        d['2330'].loc[pd.Timestamp('2026-10-20')]=d['2330'].loc[pd.Timestamp('2026-10-19')]
        d['2330']=d['2330'].drop(pd.Timestamp('2026-10-19'))
        result=run(d,e,u,CFG,'2026-10-16','2026-10-20')
        sells=[x for x in result[2] if x['side']=='SELL']
        self.assertEqual(sells[0]['date'],'2026-10-20')


class Planning(unittest.TestCase):
    def test_repeat_same_day_does_not_change_account_or_stop(self):
        idx=pd.to_datetime(['2026-10-15','2026-10-16']);f=pd.DataFrame([row(),row(raw_close=134,raw_high=135)],index=idx)
        d={'0050':f,'2330':f};e={'0050':{},'2330':{}};u={'as_of':'2026-10-16','constituents':[{'symbol':'2330','name':'test','sector':'Semi'}]}
        a=account();a['positions']['2330']=new_position(cand(),100,102,'2026-10-15',CFG)
        p,a1=make_plan(d,e,u,a,CFG,'2026-10-16');p2,a2=make_plan(d,e,u,a1,CFG,'2026-10-16')
        self.assertEqual(a1,a2);self.assertEqual(a1['cash'],a['cash']);self.assertEqual(a1['positions']['2330']['quantity'],100)
        self.assertEqual(a1['positions']['2330']['stop'],124)

    def test_entry_day_close_is_managed_for_reported_fill(self):
        cfg=json.loads((ROOT/'config.example.json').read_text())
        f=pd.DataFrame([row(raw_close=94,raw_high=103)],index=pd.to_datetime(['2026-10-19']))
        a=account();a['as_of']='2026-10-19';a['cash']=249800
        a['positions']['2330']=new_position(cand(),100,102,'2026-10-19',cfg)
        u={'as_of':'2026-10-19','constituents':[{'symbol':'2330','name':'test'}]}
        data={'0050':f,'2330':f};events={'0050':{},'2330':{}}
        plan,updated=make_plan(data,events,u,a,cfg,'2026-10-19')
        self.assertEqual(plan['actions'][0]['action'],'SELL')
        self.assertEqual(plan['actions'][0]['quantity'],100)
        _,again=make_plan(data,events,u,updated,cfg,'2026-10-19')
        self.assertEqual(updated,again)

    def test_touch_plan_renders_no_rr_cap(self):
        idx=pd.to_datetime(['2026-10-15','2026-10-16'])
        f=pd.DataFrame([row(),row(raw_close=99,rsi=20)],index=idx)
        u={'as_of':'2026-10-16','constituents':[{'symbol':'2330','name':'test','sector':'Semi'}]}
        plan,_=make_plan({'0050':f,'2330':f},{'0050':{},'2330':{}},u,account(),{**CFG,'entry_mode':'support_touch'},'2026-10-16')
        self.assertEqual(len(plan['candidates']),1)
        self.assertIn('無（仍須高於支撐）',markdown(plan))

    def test_no_sector_data_needed_for_plan_or_quotes(self):
        cfg=json.loads((ROOT/'config.example.json').read_text())
        idx=pd.to_datetime(['2026-10-15','2026-10-16'])
        f=pd.DataFrame([row(),row()],index=idx)
        # Managed stock outside the constituent universe has no sector label.
        a=account();a['cash']=249800
        a['positions']['2303']=new_position(cand('2303'),100,102,'2026-10-15',cfg)
        u={'as_of':'2026-10-16','constituents':[{'symbol':'2330','name':'test'}]}
        data={s:f.copy() for s in ['0050','2330','2303']}
        plan,updated=make_plan(data,{s:{} for s in data},u,a,cfg,'2026-10-16')
        self.assertEqual(updated['positions']['2303']['mode'],'range')
        self.assertGreater(plan['candidates'][0]['suggested_quantity'],0)
        quotes={'signal_date':'2026-10-16','trade_date':'2026-10-19','is_next_trading_session':True,
                'quotes':{s:{'open':101,'price':102} for s in ['2330','2303']}}
        result=opening_check(plan,quotes,updated,u,cfg)
        self.assertGreater(result['orders'][0]['quantity'],0)
        self.assertIn('整體初始風險 15%',markdown(plan))

    def test_quotes_cannot_spend_same_cash_twice(self):
        meta={'constituents':[{'symbol':s,'sector':s} for s in ['2330','2303']]}
        plan={'as_of':'2026-10-16','account_as_of':'2026-10-16','candidates':[cand('2330'),cand('2303')]}
        a=account();a['cash']=1000
        quotes={'signal_date':'2026-10-16','trade_date':'2026-10-19','is_next_trading_session':True,'quotes':{s:{'open':101,'price':102} for s in ['2330','2303']}}
        result=opening_check(plan,quotes,a,meta,CFG)
        self.assertGreaterEqual(result['remaining_cash'],0)
        self.assertEqual(a['cash'],1000)


if __name__=='__main__':unittest.main()
