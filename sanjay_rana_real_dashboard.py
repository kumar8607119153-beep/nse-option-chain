# -*- coding: utf-8 -*-
"""XAUT LONG + PAXG SHORT - Delta Exchange India live grid dashboard.

Real market data, L2 liquidity, account data, LIMIT-only grid orders, and a
persistent fill -> reduce-only target -> re-entry engine.

IMPORTANT: live trading is disabled until the operator explicitly checks the
live-trading confirmation and presses START LIVE GRID. Use a dedicated API key
with Read + Trading permission and a fixed-IP host. Never share API secrets.
"""
import hashlib, hmac, json, time, sqlite3, uuid, math
from decimal import Decimal, ROUND_FLOOR
from urllib.parse import urlencode
from datetime import datetime, timezone

import requests
import pandas as pd

# Streamlit Cloud may not have Plotly preinstalled when no requirements file is used.
# Bootstrap only this missing runtime dependency, keeping deployment to one .py file.
import sys
import subprocess
try:
    import plotly.graph_objects as go
except ModuleNotFoundError as exc:
    if exc.name != "plotly":
        raise
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "plotly"],
            check=True,
            timeout=180,
        )
        import plotly.graph_objects as go
    except Exception as install_error:
        raise RuntimeError(
            "Plotly is required for the candlestick chart, but automatic installation failed. "
            "The hosting environment must allow package installation and internet access. "
            f"Details: {install_error}"
        ) from install_error

import streamlit as st

BASE = "https://api.india.delta.exchange"
DB_PATH = "xaut_paxg_grid_state.sqlite3"
APP = "XAUT LONG + PAXG SHORT | DELTA INDIA REAL GRID"
D = Decimal

st.set_page_config(page_title=APP, page_icon="🟡", layout="wide", initial_sidebar_state="expanded")
st.markdown("""
<style>
.stApp{background:#080e1b;color:#e8eefb}.block-container{max-width:1600px;padding-top:1rem;padding-bottom:3rem}
[data-testid="stMetric"]{background:#101a2c;border:1px solid #273957;padding:12px;border-radius:12px}
.panel{background:#101a2c;border:1px solid #273957;border-radius:13px;padding:14px;margin:6px 0}
.small{color:#9aacc7;font-size:12px}.green{color:#26d99a}.red{color:#ff7777}
[data-testid="stDataFrame"]{border:1px solid #273957;border-radius:10px}
</style>
""", unsafe_allow_html=True)

# ----------------------- Helpers / persistent state -----------------------
def dec(v, default="0"):
    try: return D(str(v))
    except Exception: return D(default)

def jlist(resp):
    v = (resp or {}).get("result", [])
    return v if isinstance(v, list) else ([v] if v else [])

def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS bot_orders(
          client_id TEXT PRIMARY KEY, product_id INTEGER, symbol TEXT, role TEXT,
          parent_id TEXT, root_id TEXT, level_no INTEGER, cycle_no INTEGER,
          side TEXT, price TEXT, size INTEGER, covered_qty INTEGER DEFAULT 0,
          exchange_id TEXT, state TEXT DEFAULT 'submitted', created REAL)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_bot_orders_product ON bot_orders(product_id)")
        c.execute("""CREATE TABLE IF NOT EXISTS bot_config(
          k TEXT PRIMARY KEY, v TEXT)""")
        c.commit()

def db_rows(product_ids=None):
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory=sqlite3.Row
        if product_ids:
            q=','.join('?' for _ in product_ids)
            return [dict(x) for x in c.execute(f"SELECT * FROM bot_orders WHERE product_id IN ({q}) ORDER BY created", tuple(product_ids))]
        return [dict(x) for x in c.execute("SELECT * FROM bot_orders ORDER BY created")]

def save_order(row):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""INSERT OR IGNORE INTO bot_orders
        (client_id,product_id,symbol,role,parent_id,root_id,level_no,cycle_no,side,price,size,covered_qty,exchange_id,state,created)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (row['client_id'],row['product_id'],row['symbol'],row['role'],row.get('parent_id'),row.get('root_id'),
         row.get('level_no',0),row.get('cycle_no',0),row['side'],str(row['price']),int(row['size']),
         int(row.get('covered_qty',0)),str(row.get('exchange_id') or ''),row.get('state','submitted'),time.time()))
        c.commit()

def update_covered(cid, qty, state=None, exchange_id=None):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("UPDATE bot_orders SET covered_qty=?, state=COALESCE(?,state), exchange_id=COALESCE(NULLIF(?,''),exchange_id) WHERE client_id=?",
                  (int(qty),state,exchange_id or '',cid)); c.commit()

def set_cfg(k,v):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("INSERT INTO bot_config(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",(k,str(v))); c.commit()

def get_cfg(k, default=""):
    with sqlite3.connect(DB_PATH) as c:
        r=c.execute("SELECT v FROM bot_config WHERE k=?",(k,)).fetchone()
    return r[0] if r else default

def client_id(tag): return (tag[:5] + uuid.uuid4().hex)[:32]

# ----------------------- Delta REST client -----------------------
class Delta:
    def __init__(self,key="",secret=""):
        self.key,self.secret=key.strip(),secret.strip()
        self.s=requests.Session(); self.s.headers.update({"User-Agent":"xaut-paxg-live-grid/2.0","Accept":"application/json"})
    def request(self,method,path,params=None,payload=None,private=False):
        params=params or {}
        query="?"+urlencode(params,doseq=True) if params else ""
        body=json.dumps(payload,separators=(",",":"),ensure_ascii=False) if payload is not None else ""
        headers={"Content-Type":"application/json"}
        if private:
            if not self.key or not self.secret: raise RuntimeError("API Key/Secret missing")
            ts=str(int(time.time()))
            pre=method.upper()+ts+path+query+body
            sig=hmac.new(self.secret.encode(),pre.encode(),hashlib.sha256).hexdigest()
            headers.update({"api-key":self.key,"timestamp":ts,"signature":sig})
        r=self.s.request(method.upper(),BASE+path,params=params,data=body if body else None,headers=headers,timeout=(5,20))
        try: data=r.json()
        except Exception: data={"error":r.text}
        if not r.ok or data.get("success") is False:
            raise RuntimeError(f"Delta {r.status_code}: {data.get('error') or data}")
        return data
    def products(self): return self.request("GET","/v2/products",{"contract_types":"perpetual_futures","states":"live","page_size":100})
    def tickers(self): return self.request("GET","/v2/tickers",{"contract_types":"perpetual_futures"})
    def book(self,symbol,depth=50): return self.request("GET",f"/v2/l2orderbook/{symbol}",{"depth":depth})
    def candles(self,symbol,resolution="5m",limit=250):
        end=int(time.time()); seconds={"1m":60,"3m":180,"5m":300,"15m":900,"30m":1800,"1h":3600,"2h":7200,"4h":14400,"6h":21600,"1d":86400}[resolution]
        start=end-seconds*min(limit,1900)
        return self.request("GET","/v2/history/candles",{"resolution":resolution,"symbol":symbol,"start":start,"end":end})
    def balances(self): return self.request("GET","/v2/wallet/balances",private=True)
    def open_orders(self,ids): return self.request("GET","/v2/orders",{"product_ids":','.join(map(str,ids)),"state":"open","page_size":100},private=True)
    def positions(self,ids): return self.request("GET","/v2/positions/margined",{"product_ids":','.join(map(str,ids))},private=True)
    def history(self,ids):
        out=[]; params={"product_ids":','.join(map(str,ids)),"page_size":50}
        for _ in range(8):
            x=self.request("GET","/v2/orders/history",params,private=True)
            out += jlist(x)
            after=(x.get("meta") or {}).get("after")
            if not after: break
            params={**params,"after":after}
        return out
    def fills(self,ids): return self.request("GET","/v2/fills",{"product_ids":','.join(map(str,ids)),"page_size":50},private=True)
    def create_batch(self,product_id,symbol,orders):
        return self.request("POST","/v2/orders/batch",payload={"product_id":int(product_id),"product_symbol":symbol,"orders":orders},private=True)
    def create_order(self,product_id,symbol,side,price,size,cid,reduce_only=False):
        payload={"product_id":int(product_id),"side":side,"size":int(size),
                 "order_type":"limit_order","limit_price":str(price),"time_in_force":"gtc",
                 "post_only":False,"client_order_id":cid,"reduce_only":bool(reduce_only)}
        return self.request("POST","/v2/orders",payload=payload,private=True)

# ----------------------- Data and product selection -----------------------
def exact_products(products):
    by={str(p.get('symbol','')).upper():p for p in products}
    return by.get('XAUTUSD'),by.get('PAXGUSD')

def normalize_book(resp, contract_value="1"):
    x=(resp or {}).get('result') or {}
    def side(k):
        arr=[]
        for z in x.get(k,[]) or []:
            arr.append({"Price":float(dec(z.get('price'))),"Contracts":int(dec(z.get('size'))),"Depth":float(dec(z.get('depth'))),"Notional USD":int(dec(z.get('size')))*float(dec(contract_value))*float(dec(z.get('price')))})
        return pd.DataFrame(arr)
    return side('buy'),side('sell'),x.get('last_updated_at')

def candles_df(resp):
    rows=jlist(resp)
    df=pd.DataFrame(rows)
    if df.empty:return df
    df['time']=pd.to_datetime(pd.to_numeric(df['time']),unit='s',utc=True).dt.tz_convert('Asia/Kolkata')
    for k in ['open','high','low','close','volume']:
        if k in df: df[k]=pd.to_numeric(df[k],errors='coerce')
    return df.sort_values('time').drop_duplicates('time')

def make_chart(df,title,live_price,book_buy,book_sell,grid_prices,open_orders,product):
    fig=go.Figure()
    if not df.empty:
        fig.add_trace(go.Candlestick(x=df.time,open=df.open,high=df.high,low=df.low,close=df.close,name='Live OHLC'))
    if live_price:
        fig.add_hline(y=float(live_price),line_color='#f5d76e',line_width=1.5,line_dash='solid',annotation_text=f"LIVE {float(live_price):,.2f}")
    for p in grid_prices:
        fig.add_hline(y=float(p),line_color='rgba(80,170,255,.30)',line_dash='dot',line_width=1)
    # Real liquidity levels: show the strongest 12 bid/ask prices as horizontal levels.
    for frame,color,label in [(book_buy,'rgba(30,210,145,.72)','Bid liquidity'),(book_sell,'rgba(255,90,110,.72)','Ask liquidity')]:
        if not frame.empty:
            for _,r in frame.nlargest(12,'Notional USD').iterrows():
                fig.add_hline(y=r['Price'],line_color=color,line_width=1,line_dash='dash',opacity=.7,
                              annotation_text=f"{label}: ${r['Notional USD']:,.0f}",annotation_position='right')
    if not open_orders.empty:
        for _,o in open_orders.iterrows():
            try:
                col='#30d69b' if str(o.get('side')).lower()=='buy' else '#ff7070'
                fig.add_hline(y=float(o.get('limit_price')),line_color=col,line_width=2,annotation_text=f"MY {str(o.get('side')).upper()} {o.get('size')}")
            except Exception: pass
    fig.update_layout(title=title,height=530,template='plotly_dark',paper_bgcolor='#080e1b',plot_bgcolor='#080e1b',
                      xaxis_rangeslider_visible=False,margin=dict(l=10,r=20,t=45,b=10),legend=dict(orientation='h'))
    fig.update_xaxes(showgrid=True,gridcolor='#1d2a40'); fig.update_yaxes(showgrid=True,gridcolor='#1d2a40',side='right')
    return fig

def product_specs(p):
    return {"symbol":p.get('symbol'),"id":p.get('id'),"state":p.get('state'),"trading_status":p.get('trading_status'),
            "contract_value":p.get('contract_value'),"contract_unit_currency":p.get('contract_unit_currency'),
            "tick_size":p.get('tick_size'),"initial_margin":p.get('initial_margin'),"maintenance_margin":p.get('maintenance_margin'),
            "max_leverage_notional":p.get('max_leverage_notional'),"default_leverage":p.get('default_leverage'),
            "min_order_size":p.get('min_order_size'),"order_size_increment":p.get('order_size_increment'),
            "maker_fee":p.get('maker_commission_rate'),"taker_fee":p.get('taker_commission_rate')}

# ----------------------- Fill -> target -> re-entry engine -----------------------
def sync_engine(client, products_by_symbol, ids, target_gap, reentry_gap):
    """Idempotent reconciliation. It never cancels existing exchange orders."""
    open_rows=jlist(client.open_orders(ids)); hist=client.history(ids)
    # Existing resting price/side orders also serve as re-entry levels; never stack duplicates.
    resting={(int(o.get('product_id',0)),str(o.get('side','')).lower(),dec(o.get('limit_price')))
             for o in open_rows if o.get('limit_price') is not None}
    exch={}
    for o in open_rows+hist:
        cid=o.get('client_order_id')
        if cid: exch[cid]=o
    local=db_rows(ids)
    # Refresh local status from exchange snapshots.
    for r in local:
        o=exch.get(r['client_id'])
        if not o: continue
        state=o.get('state','open'); filled=max(0,int(dec(o.get('size',r['size']))-dec(o.get('unfilled_size',0))))
        update_covered(r['client_id'],r['covered_qty'],state,str(o.get('id') or ''))
        r.update(o); r['_filled']=filled
    # For every opening order, place a reduce-only target for each newly filled lot.
    for r in local:
        if r['role'] not in ('entry','reentry'): continue
        o=exch.get(r['client_id'])
        if not o: continue
        filled=max(0,int(dec(o.get('size',r['size']))-dec(o.get('unfilled_size',0))))
        already=int(r.get('covered_qty',0))
        delta_qty=filled-already
        if delta_qty<=0: continue
        p=products_by_symbol[r['symbol']]
        entry=dec(r['price']); gap=dec(target_gap)
        tp=entry+gap if r['symbol']=='XAUTUSD' else entry-gap
        tick=dec(p.get('tick_size','0.01'))
        tp=(tp/tick).quantize(D('1'),rounding=ROUND_FLOOR)*tick
        side='sell' if r['symbol']=='XAUTUSD' else 'buy'
        cid=client_id('TP')
        try:
            client.create_order(r['product_id'],r['symbol'],side,tp,delta_qty,cid,True)
            save_order({"client_id":cid,"product_id":r['product_id'],"symbol":r['symbol'],"role":"target","parent_id":r['client_id'],
                        "root_id":r.get('root_id') or r['client_id'],"level_no":r.get('level_no',0),"cycle_no":r.get('cycle_no',0),
                        "side":side,"price":tp,"size":delta_qty})
            update_covered(r['client_id'],filled)
        except Exception as e:
            st.warning(f"Target order pending for {r['symbol']} / {r['client_id']}: {e}")
    # For every target fill, re-enter at previous opening entry +/- configured gap.
    local=db_rows(ids)
    for r in local:
        if r['role']!='target': continue
        o=exch.get(r['client_id'])
        if not o: continue
        filled=max(0,int(dec(o.get('size',r['size']))-dec(o.get('unfilled_size',0))))
        already=int(r.get('covered_qty',0)); delta_qty=filled-already
        if delta_qty<=0: continue
        parent=next((x for x in local if x['client_id']==r.get('parent_id')),None)
        if not parent: continue
        entry=dec(parent['price']); gap=dec(reentry_gap)
        rp=entry-gap if r['symbol']=='XAUTUSD' else entry+gap
        p=products_by_symbol[r['symbol']]; tick=dec(p.get('tick_size','0.01'))
        rp=(rp/tick).quantize(D('1'),rounding=ROUND_FLOOR)*tick
        side='buy' if r['symbol']=='XAUTUSD' else 'sell'
        key=(int(r['product_id']),side,rp)
        if key in resting:
            # The grid already has this exact level working, so it is the re-entry order.
            update_covered(r['client_id'],filled)
            continue
        cid=client_id('RE')
        try:
            client.create_order(r['product_id'],r['symbol'],side,rp,delta_qty,cid,False)
            save_order({"client_id":cid,"product_id":r['product_id'],"symbol":r['symbol'],"role":"reentry","parent_id":r['client_id'],
                        "root_id":r.get('root_id') or parent.get('root_id') or parent['client_id'],"level_no":r.get('level_no',0),
                        "cycle_no":int(r.get('cycle_no',0))+1,"side":side,"price":rp,"size":delta_qty})
            resting.add(key)
            update_covered(r['client_id'],filled)
        except Exception as e:
            st.warning(f"Re-entry order pending for {r['symbol']} / {r['client_id']}: {e}")
    return open_rows,hist

# ----------------------- Sidebar -----------------------
init_db()
st.title("🟡 XAUT LONG + PAXG SHORT — REAL DELTA GRID")
st.caption("दो स्वतंत्र कॉन्ट्रैक्ट • अलग लाइव चार्ट • वास्तविक L2 Liquidity • LIMIT-only orders • Persistent target/re-entry tracking")
with st.sidebar:
    st.subheader("Delta India API")
    api_key=st.text_input("API Key",type="password",key="delta_key")
    api_secret=st.text_input("API Secret",type="password",key="delta_secret")
    st.caption("API secret chat/GitHub में न भेजें। Trading permission + IP whitelist जरूरी है।")
    st.divider(); st.subheader("Grid Configuration")
    low=dec(st.number_input("LOW price",value=3000.0,step=10.0))
    high=dec(st.number_input("HIGH price",value=4800.0,step=10.0))
    gap=dec(st.number_input("Grid gap ($)",value=10.0,step=1.0))
    lots=st.number_input("Lots per grid level",min_value=1,value=1,step=1)
    target_gap=dec(st.number_input("Target distance ($)",value=10.0,step=1.0))
    reentry_gap=dec(st.number_input("Re-entry distance ($)",value=10.0,step=1.0))
    resolution=st.selectbox("Chart timeframe",["1m","3m","5m","15m","30m","1h","2h","4h","6h","1d"],index=2)
    depth=st.select_slider("Liquidity depth",options=[10,20,30,50,100],value=50)
    auto_sync=st.toggle("Auto monitor target / re-entry",value=True)
    refresh_seconds=st.select_slider("Refresh seconds",options=[5,8,10,15,30],value=8)

# Validate settings and schedule a full-page refresh so market data and the
# persistent fill manager are polled even when the user does not press Refresh.
if gap<=0 or high<low or (high-low)%gap!=0:
    st.error("Grid range invalid: HIGH must be >= LOW and (HIGH-LOW) must divide evenly by Grid gap.")
    st.stop()
levels=[low+gap*i for i in range(int((high-low)/gap)+1)]
try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=int(refresh_seconds)*1000, key="xaut_paxg_full_refresh")
except ImportError:
    st.info("Auto refresh package missing. Install requirements_XAUT_PAXG.txt; until then use the Refresh button.")
    if st.button("Refresh live data now"):
        st.rerun()

if not api_key or not api_secret:
    st.warning("API credentials डालें। Credentials के बिना केवल कोई live/private डेटा नहीं दिखेगा; नकली डेटा नहीं बनाया जाएगा।")
    st.info(f"Configured grid: {len(levels)} levels per contract, {len(levels)*2} initial LIMIT orders total, {lots} lot(s) each.")
    st.stop()

client=Delta(api_key,api_secret)
try:
    prod_resp=client.products(); ticker_resp=client.tickers()
    all_products=jlist(prod_resp); xp,pp=exact_products(all_products)
    ticker_map={str(x.get('product_id')):x for x in jlist(ticker_resp)}
    if not xp or not pp:
        st.error("Delta India की live products list में exact XAUTUSD और PAXGUSD दोनों नहीं मिले। कोई alias या दूसरा symbol मानकर ऑर्डर नहीं भेजे जाएँगे।")
        st.dataframe(pd.DataFrame([product_specs(x) for x in all_products if any(k in str(x.get('symbol','')).upper() for k in ('XAU','PAX'))]),use_container_width=True)
        st.stop()
    for p in (xp,pp):
        if p.get('state')!='live' or p.get('trading_status') not in (None,'operational'):
            st.error(f"{p.get('symbol')} trading state operational नहीं है; live orders रोक दिए गए।")
            st.stop()
    prods={xp['symbol']:xp,pp['symbol']:pp}; ids=[int(xp['id']),int(pp['id'])]
except Exception as e:
    st.error(f"Delta API connection error: {e}"); st.stop()

# private account snapshot
try:
    balances=jlist(client.balances()); open_all=jlist(client.open_orders(ids)); positions=jlist(client.positions(ids)); fills=jlist(client.fills(ids))
except Exception as e:
    st.error(f"Private API read failed. Check Read/Trading permission and IP whitelist. {e}"); st.stop()

# product overview
c1,c2,c3,c4=st.columns(4)
for col,p in zip((c1,c2), (xp,pp)):
    t=ticker_map.get(str(p['id']),{})
    col.metric(f"{p['symbol']} Live Price",f"${dec(t.get('close') or t.get('mark_price')):,.2f}",f"Mark ${dec(t.get('mark_price')):,.2f}")
c3.metric("Initial Grid Orders",f"{len(levels)*2}",f"{lots} lot(s) × {len(levels)} each")
c4.metric("Live Open Orders",len(open_all),"XAUTUSD + PAXGUSD")

# Explicit start / arm controls
st.divider(); st.subheader("LIVE GRID CONTROL")
existing_bot_orders=db_rows(ids)
armed=st.checkbox("मैं समझता/समझती हूँ कि START दबाने पर Delta Exchange India पर वास्तविक पैसे से LIMIT orders भेजे जाएँगे।",value=False)
confirm_text=st.text_input("Live start confirm करने के लिए START LIVE लिखें",placeholder="START LIVE")
start_col, status_col=st.columns([1,2])
with start_col:
    start_live=st.button("🚀 START LIVE GRID — REAL ORDERS",type="primary",use_container_width=True,disabled=not(armed and confirm_text.strip()=="START LIVE"))
with status_col:
    st.write(f"Grid range ${low:,.2f}–${high:,.2f} | gap ${gap} | Target ${target_gap} | Re-entry ${reentry_gap}")
if start_live:
    # Validate order size increments from live metadata when present.
    errors=[]
    for p in (xp,pp):
        inc=int(dec(p.get('order_size_increment') or 1)); minimum=int(dec(p.get('min_order_size') or 1))
        if lots<minimum or lots%max(1,inc)!=0: errors.append(f"{p['symbol']}: lot size {lots} min={minimum}, increment={inc}")
    if errors:
        st.error("Live order size validation failed: "+"; ".join(errors))
    else:
        # Do not duplicate any price/side already resting on exchange or in our local ledger.
        placed=0; skipped=0; errors=[]
        open_keys={(int(o.get('product_id',0)),str(o.get('side','')).lower(),dec(o.get('limit_price'))) for o in open_all}
        local_keys={(int(r['product_id']),r['side'],dec(r['price'])) for r in existing_bot_orders if r['state'] not in ('cancelled','closed')}
        for p,side in ((xp,'buy'),(pp,'sell')):
            batch=[]; rows=[]
            for i,price in enumerate(levels):
                key=(int(p['id']),side,price)
                if key in open_keys or key in local_keys:
                    skipped+=1; continue
                cid=client_id('GX' if side=='buy' else 'PS')
                batch.append({"limit_price":str(price),"size":int(lots),"side":side,"order_type":"limit_order",
                              "time_in_force":"gtc","post_only":False,"mmp":"disabled","client_order_id":cid})
                rows.append({"client_id":cid,"product_id":int(p['id']),"symbol":p['symbol'],"role":"entry","parent_id":None,
                             "root_id":cid,"level_no":i+1,"cycle_no":0,"side":side,"price":price,"size":int(lots)})
            for ix in range(0,len(batch),50):
                b=batch[ix:ix+50]; rr=rows[ix:ix+50]
                try:
                    result=client.create_batch(p['id'],p['symbol'],b)
                    accepted=jlist(result)
                    if accepted:
                        accepted_ids={str(x.get('client_order_id')) for x in accepted if x.get('client_order_id')}
                        for row in rr:
                            if not accepted_ids or row['client_id'] in accepted_ids: save_order(row); placed+=1
                    else:
                        # Confirm via active orders before persisting any unacknowledged batch.
                        check=jlist(client.open_orders([int(p['id'])])) + client.history([int(p['id'])]); got={x.get('client_order_id') for x in check}
                        for row in rr:
                            if row['client_id'] in got: save_order(row); placed+=1
                        if not got: errors.append(f"{p['symbol']} batch returned no identifiable accepted orders: {result}")
                except Exception as e: errors.append(f"{p['symbol']} batch: {e}")
        st.success(f"Delta ने {placed} initial LIMIT orders स्वीकार किए। Existing/duplicate skipped: {skipped}.")
        if errors: st.error("कुछ batch fail हुए: "+" | ".join(errors))
        st.rerun()

# Refresh private data after start action / engine monitor
if auto_sync:
    try:
        open_all, hist=sync_engine(client,prods,ids,target_gap,reentry_gap)
        # refresh account data after potential target/re-entry creation
        open_all=jlist(client.open_orders(ids)); positions=jlist(client.positions(ids)); fills=jlist(client.fills(ids))
        st.caption(f"Auto monitor active • checked {datetime.now().strftime('%H:%M:%S')} IST • target/re-entry reconciliation runs on each refresh")
    except Exception as e:
        st.warning(f"Auto monitor could not complete this cycle: {e}")

# Chart panels and liquidity
st.divider(); st.subheader("TWO SEPARATE LIVE CHARTS + REAL ORDER-BOOK LIQUIDITY")
for p,side_name in ((xp,'XAUT LONG / BUY'),(pp,'PAXG SHORT / SELL')):
    symbol=p['symbol']; t=ticker_map.get(str(p['id']),{}); price=dec(t.get('close') or t.get('mark_price'))
    try:
        bresp=client.book(symbol,depth); bids,asks,updated=normalize_book(bresp,p.get('contract_value') or '1')
        cdf=candles_df(client.candles(symbol,resolution,300))
    except Exception as e:
        st.error(f"{symbol} market data error: {e}"); continue
    my_open=pd.DataFrame([o for o in open_all if int(o.get('product_id',0))==int(p['id'])])
    with st.container(border=True):
        st.markdown(f"### {'🟢' if symbol=='XAUTUSD' else '🔴'} {side_name} — {symbol}")
        m1,m2,m3,m4=st.columns(4)
        m1.metric("Last Price",f"${price:,.2f}")
        m2.metric("Best Bid",f"${dec(bids.iloc[0]['Price'] if not bids.empty else 0):,.2f}")
        m3.metric("Best Ask",f"${dec(asks.iloc[0]['Price'] if not asks.empty else 0):,.2f}")
        m4.metric("Spread",f"${(dec(asks.iloc[0]['Price'])-dec(bids.iloc[0]['Price'])) if not bids.empty and not asks.empty else D('0'):,.2f}")
        st.plotly_chart(make_chart(cdf,f"{symbol} • {resolution} Candles + live liquidity bands",price,bids,asks,levels,my_open,p),use_container_width=True,config={"scrollZoom":False,"displaylogo":False})
        lcol,rcol=st.columns(2)
        with lcol:
            st.markdown("**🟢 BUY / BID LIQUIDITY — वास्तविक खरीद ऑर्डर**")
            if bids.empty: st.info("No bid depth returned by exchange")
            else: st.dataframe(bids.sort_values('Price',ascending=False),use_container_width=True,hide_index=True,height=260)
        with rcol:
            st.markdown("**🔴 SELL / ASK LIQUIDITY — वास्तविक बिक्री ऑर्डर**")
            if asks.empty: st.info("No ask depth returned by exchange")
            else: st.dataframe(asks.sort_values('Price'),use_container_width=True,hide_index=True,height=260)
        if updated: st.caption(f"Order book exchange timestamp (µs): {updated} • Depth levels: {depth} each side")

# Account panels
st.divider(); st.subheader("REAL ACCOUNT — BALANCE / POSITIONS / ORDERS / FILLS")
with st.expander("Wallet balances",expanded=True):
    st.dataframe(pd.DataFrame([{"Asset":x.get('asset_symbol'),"Balance":x.get('balance'),"Available":x.get('available_balance'),
        "Order Margin":x.get('order_margin'),"Position Margin":x.get('position_margin'),"Blocked Margin":x.get('blocked_margin')} for x in balances]),use_container_width=True,hide_index=True)
with st.expander("XAUT + PAXG positions",expanded=True):
    st.dataframe(pd.DataFrame(positions),use_container_width=True,hide_index=True)
with st.expander("Open orders — only these two products",expanded=True):
    st.dataframe(pd.DataFrame(open_all),use_container_width=True,hide_index=True)
with st.expander("Recent fills / executed trades",expanded=False):
    st.dataframe(pd.DataFrame(fills),use_container_width=True,hide_index=True)
with st.expander("Local bot order lifecycle (entry → target → re-entry)",expanded=False):
    local=db_rows(ids)
    st.dataframe(pd.DataFrame(local),use_container_width=True,hide_index=True)
with st.expander("Live contract specs / fees / margin",expanded=False):
    st.dataframe(pd.DataFrame([product_specs(xp),product_specs(pp)]),use_container_width=True,hide_index=True)

st.divider(); st.subheader("BOT STATUS & IMPORTANT OPERATING NOTES")
local=db_rows(ids)
role_counts={r:sum(1 for x in local if x['role']==r) for r in ('entry','target','reentry')}
a,b,c=st.columns(3); a.metric("Tracked Entry Orders",role_counts['entry']); b.metric("Tracked Target Orders",role_counts['target']); c.metric("Tracked Re-entry Orders",role_counts['reentry'])
st.info("Bot local state SQLite file में सेव होता है। उसी मशीन/फोल्डर से ऐप चलाएँ ताकि target/re-entry tracking बनी रहे। Dashboard/Streamlit process चालू रहना जरूरी है; इसे बंद करने पर automatic monitoring रुक जाएगा, लेकिन Delta पर पहले से लगे exchange orders वहीं रहेंगे।")
st.warning("यह strategy 2×181 = 362 initial orders तक भेज सकती है (आपकी range के अनुसार)। हर order पर margin/fees/funding/liquidation risk है। Exchange का reduce-only target protective है, लेकिन fill timing, partial fills और network outages के कारण execution risk खत्म नहीं होता। पहले कम grid levels और छोटी size से test करें।")
st.caption("Official Delta India API: https://docs.delta.exchange/ • Production: https://api.india.delta.exchange • Orders are LIMIT only; no market order and no automatic cancellation of pre-existing orders.")

