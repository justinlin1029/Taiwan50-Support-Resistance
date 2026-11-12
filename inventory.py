"""User-reported positions are truth. Atomic writes, backups, idempotent fills."""
import copy
import fcntl
import json
import math
import os
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
from strategy import new_position,commission


def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    with temporary.open('w') as f:
        json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False);f.flush();os.fsync(f.fileno())
    os.replace(temporary,path)


@contextmanager
def transaction(root):
    with (root/'.account.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        original=json.loads((root/'account.json').read_text())
        account=copy.deepcopy(original)
        yield account
        if account!=original:
            validate(account)
            stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            write_json(root/'inventory_history'/f'{stamp}.json',original)
            write_json(root/'account.json',account)


def positive(value,zero=False):
    return isinstance(value,(float,int)) and math.isfinite(value) and (value>=0 if zero else value>0)


def validate(account):
    if not positive(account['cash'],True):raise ValueError('可用現金不得為負數或非有限數值')
    datetime.strptime(account['as_of'],'%Y-%m-%d')
    for sym,p in account['positions'].items():
        if not isinstance(sym,str) or not sym.isdigit():raise ValueError('代號必須為字串，例如 "2330"')
        if not positive(p['quantity']) or p['quantity']!=int(p['quantity']):raise ValueError('實際庫存股數必須為正整數')
        if not positive(p['avg_cost']):raise ValueError('平均成本必須大於零')
        if p.get('mode') not in ['range','trend','unmanaged']:raise ValueError('持倉模式無效')
        if p['mode']!='unmanaged':
            for k in ['support','resistance','stop','initial_risk','highest_close']:
                if not positive(p.get(k),k=='initial_risk'):raise ValueError(f'{sym}: 缺少有效的 {k}')
            if not p['support']<p['resistance']:raise ValueError('原始支撐必須低於原始壓力')


def reconcile(account,snapshot):
    """A complete snapshot, explicitly declared. Never infer cash from share changes."""
    if snapshot.get('complete_snapshot') is not True:raise ValueError('請加 complete_snapshot: true，確認這是完整庫存清單')
    if snapshot['as_of']<account['as_of']:raise ValueError('不能匯入比目前帳戶更早的庫存快照')
    if not positive(snapshot['cash'],True):raise ValueError('現金無效')
    positions={};changes=[]
    for item in snapshot['positions']:
        s=item['symbol']
        if s in positions:raise ValueError('重複股票代號')
        q=item['quantity'];cost=item['avg_cost']
        if not positive(q,True) or q!=int(q) or not positive(cost):raise ValueError('股數／成本格式錯誤')
        if q==0:continue
        old=account['positions'].get(s)
        if old:
            p=copy.deepcopy(old)
            # Extra shares not justified by original plan need review.
            if q>p['quantity'] or abs(cost-p['avg_cost'])>max(.01,p['avg_cost']*.0001):
                p['mode']='unmanaged';p['review_reason']='股數增加或成本改變，需確認成交／公司行動與停損'
            else:p['initial_risk']=p.get('initial_risk',0)*q/p['quantity']
            p['quantity']=q;p['avg_cost']=cost
        else:p={'quantity':q,'avg_cost':cost,'mode':'unmanaged','initial_risk':0,'entry_date':snapshot['as_of'],'basis_date':snapshot['as_of']}
        if 'sector' in item:p['sector']=item['sector']
        positions[s]=p
    for s in sorted(set(account['positions'])|set(positions)):
        before=account['positions'].get(s,{}).get('quantity',0);after=positions.get(s,{}).get('quantity',0)
        if before!=after:changes.append({'symbol':s,'before':before,'after':after})
    account['positions']=positions;account['cash']=float(snapshot['cash']);account['as_of']=snapshot['as_of'];account['cash_is_approximate']=bool(snapshot.get('cash_is_approximate',False))
    account.setdefault('audit',[]).append({'type':'snapshot','date':snapshot['as_of'],'changes':changes,'cash':account['cash']})
    validate(account)
    return changes


def apply_fills(account,fills,cfg,plan=None):
    trial=copy.deepcopy(account)
    candidates={x['symbol']:x for x in (plan or {}).get('candidates',[])}
    for fill in fills:
        ident=fill['fill_id']
        if ident in trial.setdefault('processed_fill_ids',[]):continue
        if fill['date']<trial['as_of']:raise ValueError('成交日期早於已對帳庫存；不可重複套用舊成交')
        s=fill['symbol'];q=fill['quantity'];price=fill['price'];side=fill['side'].upper()
        if not isinstance(s,str) or not s.isdigit() or not positive(q) or q!=int(q) or not positive(price):raise ValueError('成交代號／數量／價格無效')
        amount=q*price;fee=fill.get('fee',commission(amount,cfg));tax=fill.get('tax',amount*cfg['sell_tax'] if side=='SELL' else 0)
        if not positive(fee,True) or not positive(tax,True):raise ValueError('費用不可為負數')
        if side=='BUY':
            if amount+fee+tax>trial['cash']+1e-8:raise ValueError('成交金額超過紀錄現金；先回報入金或正確現金')
            trial['cash']-=amount+fee+tax
            if s in trial['positions']:
                p=trial['positions'][s];total=p['quantity']+q;p['avg_cost']=(p['quantity']*p['avg_cost']+amount)/total;p['quantity']=total;p['mode']='unmanaged'
            elif s in candidates and 0<(datetime.fromisoformat(fill['date'])-datetime.fromisoformat(plan['as_of'])).days<=7 and not fill.get('manual',False):
                p=new_position(candidates[s],q,price,fill['date'],cfg)
                p['entry_cost']=amount+fee+tax
                maximum=candidates[s]['max_buy_price']
                if price<=candidates[s]['support'] or (maximum is not None and price>maximum):p['mode']='unmanaged'
                trial['positions'][s]=p
            else:trial['positions'][s]={'quantity':q,'avg_cost':price,'entry_date':fill['date'],'basis_date':fill['date'],'mode':'unmanaged','initial_risk':0}
        elif side=='SELL':
            if s not in trial['positions'] or q>trial['positions'][s]['quantity']:raise ValueError('賣出股數超過實際庫存')
            trial['cash']+=amount-fee-tax;p=trial['positions'][s];fraction=q/p['quantity']
            if q==p['quantity']:del trial['positions'][s]
            else:
                p['quantity']-=q
                for k in ['initial_risk','entry_cost','dividends']:
                    if k in p:p[k]*=1-fraction
        else:raise ValueError('side 必須是 BUY 或 SELL')
        trial['processed_fill_ids'].append(ident);trial['as_of']=fill['date']
        trial.setdefault('audit',[]).append({'type':'fill',**fill,'recorded_fee':fee,'recorded_tax':tax,'fees_estimated':'fee' not in fill or 'tax' not in fill})
    validate(trial);account.clear();account.update(trial)


def adopt(account,spec,cfg):
    s=spec['symbol'];p=account['positions'][s]
    if spec['as_of']!=account['as_of']:raise ValueError('管理設定日期必須與庫存快照日期一致')
    required=['support','resistance','stop','mode']
    for k in required:
        if k not in spec:raise ValueError(f'缺少 {k}')
    if spec['mode'] not in ['range','trend']:raise ValueError('mode 必須是 range 或 trend')
    p.update({k:spec[k] for k in required});p['highest_close']=float(spec.get('highest_close',p['avg_cost']))
    p['initial_stop']=spec['stop'];p['initial_risk']=p['quantity']*max(p['avg_cost']-p['stop'],0)+2*commission(p['quantity']*p['avg_cost'],cfg)+p['quantity']*p['avg_cost']*cfg['sell_tax']
    p['basis_date']=spec['as_of'];p['last_managed_date']=spec['as_of'];p['pending_exit']=None
    account.setdefault('audit',[]).append({'type':'adopt','spec':spec})
    validate(account)
