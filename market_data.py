"""Free public data with cached source responses and strict date checks."""
import concurrent.futures
import hashlib
import json
import os
import re
from pathlib import Path
import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

API='https://api.finmindtrade.com/api/v4/data'


def fetch(dataset, symbol=None, start=None, end=None):
    params={'dataset':dataset}
    if symbol:params['data_id']=symbol
    if start:params['start_date']=start
    if end:params['end_date']=end
    headers={}
    if os.getenv('FINMIND_TOKEN'):headers['Authorization']='Bearer '+os.environ['FINMIND_TOKEN']
    r=requests.get(API,params=params,headers=headers,timeout=40)
    r.raise_for_status();j=r.json()
    if j.get('status')!=200:raise ValueError(f'{dataset}/{symbol}: {j.get("msg")}')
    return j


def _yuanta_stock_rows(soup):
    """Read the full published stock array when only five cards are visible.

    Decode literal arguments only; never execute the page's JavaScript.
    """
    for tag in soup.find_all('script'):
        script=tag.get_text().strip()
        header=re.match(r'window\.__NUXT__=\(function\(([^)]*)\)',script)
        boundary=script.rfind('}(')
        if not header or boundary<0 or not script.endswith('));'):continue
        args=script[boundary+2:-3]
        # Preserve quoted strings; normalize JS fractional literals. Array
        # placeholders belong to unrelated menus and need no interpretation.
        args=re.sub(r'("(?:\\.|[^"\\])*")|Array\(\d+\)|(?<![\w.])(-?)\.(\d+)',
                    lambda m:m[1] if m[1] is not None else 'null' if m[2] is None else m[2]+'0.'+m[3],args)
        try:
            values=json.loads('['+args+']');names=header.group(1).split(',')
            if len(names)!=len(values):continue
            bindings=dict(zip(names,values))
            stocks=re.search(r'StockWeights:\[(.*?)\]',script[:boundary])
            if not stocks:continue
            fields=re.findall(r'\{code:([\w$]+),ym:[^,]+,name:([\w$]+),ename:[^,]+,weights:([^,]+),qty:([^}]+)\}',stocks.group(1))
            rows=[[bindings[code],bindings[name]] for code,name,_,_ in fields]
            if len(rows)==50 and len({x[0] for x in rows})==50 and all(isinstance(x[0],str) and re.fullmatch(r'\d{4}',x[0]) for x in rows):return rows
        except (ValueError,KeyError,TypeError):continue
    return []


def refresh_universe(root):
    # Official endpoint is attempted first; fallback is explicitly recorded.
    urls=['https://www.yuantaetfs.com/product/detail/0050/ratio','https://www.jojoradar.com/0050']
    selected=None
    for url in urls:
        try:r=requests.get(url,timeout=30);r.raise_for_status()
        except requests.RequestException:continue
        soup=BeautifulSoup(r.text,'html.parser');rows=[];official_date=None
        for table in soup.find_all('table'):
            found=[]
            for tr in table.find_all('tr'):
                cells=[td.get_text(' ',strip=True) for td in tr.find_all('td')]
                if cells and re.fullmatch(r'\d{4}',cells[0]):found.append(cells)
            if len(found)==50:rows=found;break
        # Yuanta's responsive holdings page also renders labelled cards, not tables.
        if not rows and url.startswith('https://www.yuantaetfs.com/'):
            visible=soup.get_text(' ',strip=True)
            found=re.findall(r'商品代碼\s+(\d{4})\s+商品名稱\s+(.+?)\s+商品數量\s+([\d,]+)\s+商品權重\s+([\d.]+)',visible)
            dated=re.search(r'基金權重-股票\s+交易日期\s*[:：]\s*(20\d{2}/\d{2}/\d{2})',visible)
            if len(found)!=50:
                full=_yuanta_stock_rows(soup)
                if full and {x[0] for x in found}<={x[0] for x in full}:found=full
            if len(found)==50 and len({x[0] for x in found})==50 and dated:
                rows=found;official_date=dated.group(1).replace('/','-')
        if rows:
            dates=re.findall(r'20\d{2}[-/]\d{2}[-/]\d{2}',soup.get_text())
            if not dates:raise ValueError('成分股頁缺少資料日期')
            selected=(url,official_date or dates[0].replace('/','-'),rows);break
    if selected is None:raise ValueError('無法取得完整 50 檔名單；保留舊檔，請手動匯入 universe.json')
    info=pd.DataFrame(fetch('TaiwanStockInfo')['data']).sort_values('date').drop_duplicates('stock_id',keep='last').set_index('stock_id')
    url,date,rows=selected
    items=[]
    for row in rows:
        code=row[0]
        if code not in info.index:raise ValueError(f'{code} 無產業資料')
        items.append({'symbol':code,'name':str(info.loc[code,'stock_name']),'sector':str(info.loc[code,'industry_category'])})
    path=root/'data/universe.json'
    if path.exists():path.with_suffix('.previous.json').write_text(path.read_text())
    old=json.loads(path.read_text()) if path.exists() else {}
    old_current={x['symbol'] for x in old.get('constituents',[]) if x.get('valid_from','0000')<=old.get('as_of','')<=x.get('valid_to','9999')}
    if old_current=={x['symbol'] for x in items}:
        old.update(as_of=date,source=url)
        payload=old
    else:
        payload={'as_of':date,'source':url,'mode':'current_snapshot','constituents':items,'history_note':'成分名單變更尚未核實生效日；禁止假裝為歷史成分名單'}
    text=json.dumps(payload,ensure_ascii=False,indent=2)
    path.write_text(text)
    snapshots=root/'data/universe_snapshots';snapshots.mkdir(exist_ok=True)
    (snapshots/f'{date}.json').write_text(text)


def download(root,start,end):
    u=json.loads((root/'data/universe.json').read_text())
    codes=sorted({x['symbol'] for x in u['constituents']}|{'0050'})
    account=root/'account.json'
    if account.exists():codes=sorted(set(codes)|set(json.loads(account.read_text())['positions']))
    raw=root/'data/raw';raw.mkdir(parents=True,exist_ok=True)
    def one(code):
        import yfinance as yf
        for ds in ['TaiwanStockPrice','TaiwanStockDividendResult','TaiwanStockDividend']:
            data=fetch(ds,code,start,end)
            (raw/f'{code}_{ds}.json').write_text(json.dumps(data,ensure_ascii=False))
        df=yf.Ticker(code+'.TW').history(start=start,end=str((pd.Timestamp(end)+pd.Timedelta(days=1)).date()),auto_adjust=False,actions=True)
        if df.empty:raise ValueError(f'{code}: Yahoo 公司行動資料為空')
        df.reset_index().to_json(raw/f'{code}_yahoo.json',orient='table',date_format='iso')
        return code
    errors=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        jobs={pool.submit(one,c):c for c in codes}
        for future in concurrent.futures.as_completed(jobs):
            try:print('downloaded',future.result(),flush=True)
            except Exception as e:errors.append(f'{jobs[future]}: {e}')
    if errors:raise ValueError('\n'.join(errors))


def prepare(root):
    raw=root/'data/raw';out=root/'data/prepared';out.mkdir(exist_ok=True)
    quality=[];manifest=[]
    for path in sorted(raw.glob('*_TaiwanStockPrice.json')):
        code=path.name.split('_')[0]
        def read(ds):return json.loads((raw/f'{code}_{ds}.json').read_text())['data']
        f=pd.DataFrame(read('TaiwanStockPrice'))
        if f.empty:raise ValueError(f'{code}: no prices')
        f=f.rename(columns={'max':'high','min':'low','Trading_Volume':'volume'})
        f['date']=pd.to_datetime(f['date']);f=f.sort_values('date').set_index('date')
        if f.index.has_duplicates:raise ValueError(f'{code}: duplicate daily prices')
        valid=(f[['open','high','low','close']]>0).all(axis=1)&(f['volume']>0)
        quality.extend({'symbol':code,'date':str(d.date()),'issue':'non-trading/invalid row omitted'} for d in f.index[~valid])
        f=f.loc[valid].copy()
        malformed=(f.high<f[['open','close','low']].max(axis=1))|(f.low>f[['open','close','high']].min(axis=1))
        if malformed.any():
            last_bad=f.index[malformed][-1]
            quality.append({'symbol':code,'date':str(last_bad.date()),'issue':'inconsistent OHLC (e.g. emerging-market price semantics); discard history through this date'})
            f=f.loc[f.index>last_bad].copy()
            if f.empty:raise ValueError(f'{code}: no valid history after malformed OHLC')
        events={}
        def event(d):return events.setdefault(d,{'cash':0.,'split':1.,'factor':1.,'pay_date':None,'unsupported':False,'bonus':False})
        policies=read('TaiwanStockDividend')
        for r in policies:
            cash=float(r['CashEarningsDistribution'])+float(r['CashStatutorySurplus'])
            if cash>0 and r['CashExDividendTradingDate']:
                e=event(r['CashExDividendTradingDate']);e['cash']=cash;e['pay_date']=r.get('CashDividendPaymentDate') or None
            if float(r['StockEarningsDistribution'])+float(r['StockStatutorySurplus'])>0 and r['StockExDividendTradingDate']:
                event(r['StockExDividendTradingDate'])['bonus']=True
            if float(r.get('CashIncreaseSubscriptionRate',0))>0 and r['StockExDividendTradingDate']:
                event(r['StockExDividendTradingDate'])['unsupported']=True
        yahoo=pd.read_json(raw/f'{code}_yahoo.json',orient='table')
        yahoo['day']=pd.to_datetime(yahoo['Date']).dt.tz_convert('Asia/Taipei').dt.strftime('%Y-%m-%d')
        for _,r in yahoo.iterrows():
            if r.get('Stock Splits',0)>0:event(r.day)['split']=float(r['Stock Splits'])
            if r.get('Dividends',0)>0 and r.day not in events:
                # Unknown payment date: receivable remains nonspendable to end.
                event(r.day)['cash']=float(r['Dividends'])
        for r in read('TaiwanStockDividendResult'):
            e=event(r['date'])
            if r['before_price']<=0 or r['reference_price']<=0:raise ValueError('Invalid corporate action factor')
            e['factor']=float(r['before_price'])/float(r['reference_price'])
            if '權' in r['stock_or_cache_dividend'] and e['split']==1 and not e['unsupported']:
                e['unsupported']=True
            if r['stock_or_cache_dividend']=='息' and e['cash']==0:
                e['cash']=float(r['stock_and_cache_dividend'])
        for d,e in events.items():
            if e['factor']==1 and e['split']!=1:e['factor']=e['split']
        # Yahoo sometimes dates a split at the start of a trading suspension.
        # Apply it on first available trading day, not silently drop the event.
        aligned={}
        for ds,e in sorted(events.items()):
            after=f.index[f.index>=pd.Timestamp(ds)]
            if not len(after):continue
            target=str(after[0].date())
            if target not in aligned:aligned[target]=dict(e)
            else:
                prior=aligned[target]
                prior['cash']+=e['cash'];prior['split']*=e['split']
                prior['factor']=max(prior['factor'],e['factor'])
                prior['pay_date']=e['pay_date'] or prior['pay_date']
                prior['unsupported']|=e['unsupported'];prior['bonus']|=e['bonus']
        events=aligned
        # Explicit, documented correction for an action missing from Yahoo.
        overrides=root/'data/action_overrides.json'
        if overrides.exists():
            for ds,override in json.loads(overrides.read_text()).get(code,{}).items():
                event(ds).update({k:v for k,v in override.items() if k!='source'})
        factor=1.;split_factor=1.;f['factor']=1.;f['volume_factor']=1.
        last_close=None
        for d,row in f.iterrows():
            ds=str(d.date());e=events.get(ds)
            if e:factor*=e['factor'];split_factor*=e['split']
            # A huge unrecognized gap can be a split/capital reduction; don't fabricate.
            if last_close and abs(row.open*(e['factor'] if e else 1)/last_close-1)>.35:
                quality.append({'symbol':code,'date':ds,'issue':'unexplained >35% gap; indicator reset'})
                event(ds)['unsupported']=True;event(ds)['reset_indicator']=True
            f.loc[d,'factor']=factor;f.loc[d,'volume_factor']=split_factor
            last_close=row.close
        for key in ['open','high','low','close']:
            f['raw_'+key]=f[key];f[key]=f[key]*f.factor
        f['raw_volume']=f.volume;f['volume']=f.volume/f.volume_factor
        columns=['open','high','low','close','volume','factor','raw_open','raw_high','raw_low','raw_close','raw_volume','volume_factor']
        payload={'symbol':code,'rows':[{**{'date':str(d.date())},**{k:float(r[k]) for k in columns}} for d,r in f.iterrows()],'events':events}
        (out/f'{code}.json').write_text(json.dumps(payload,ensure_ascii=False))
        manifest.append({'symbol':code,'first':str(f.index[0].date()),'last':str(f.index[-1].date()),'rows':len(f),'raw_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    (root/'data/quality.json').write_text(json.dumps(quality,ensure_ascii=False,indent=2))
    (root/'data/manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    return quality


def load(root,cfg):
    from indicators import compute
    data={};events={}
    for p in sorted((root/'data/prepared').glob('*.json')):
        j=json.loads(p.read_text());f=pd.DataFrame(j['rows']);f['date']=pd.to_datetime(f.date);f=f.set_index('date')
        # Reset on unsupported actions, including old unknown splits. Do not carry
        # structural levels across such events. New entries need fresh warmup.
        breaks=sorted(pd.Timestamp(d) for d,e in j['events'].items() if e.get('reset_indicator',False) and pd.Timestamp(d) in f.index)
        segments=[];start=f.index[0]
        for b in breaks:
            segment=f.loc[(f.index>=start)&(f.index<b)]
            if len(segment):segments.append(compute(segment,cfg))
            start=b
        if len(f.loc[start:]):segments.append(compute(f.loc[start:],cfg))
        data[p.stem]=pd.concat(segments);events[p.stem]=j['events']
    return data,events
