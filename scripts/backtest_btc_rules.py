"""
Backtest aturan masuk auto trader BTC (1 jam & 15 menit) dengan harga Polymarket historis.

Per market: satu entri pada menit pertama di jendela masuk yang memenuhi aturan (seperti bot live).
Harga beli = harga CLOB per menit SESUDAH keputusan (konservatif) + 1¢ setengah spread + fee taker
0.07·p·(1−p). Model sama dengan autotrader.btc_model. ROI juga dibagi dua paruh waktu untuk cek konsistensi.

Aturan yang diuji: aturan sekarang (edge ≥ 5¢), + harga minimum 30/40/50¢, edge ≥ 10¢, dan beli favorit pasar.

    python scripts/backtest_btc_rules.py 14
"""
import sys,json,math,statistics
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_btc_hourly as B
from pathlib import Path
from datetime import datetime,timedelta,timezone
from concurrent.futures import ThreadPoolExecutor
cache=Path(__file__).resolve().parent.parent/'.backtest_cache'/'btc'; cache.mkdir(parents=True,exist_ok=True)
DAYS=int(sys.argv[1]) if len(sys.argv)>1 else 14
def load_event15(start):
    slug=f"btc-updown-15m-{int(start.timestamp())}"
    ev=B.cached(cache,f"ev_{slug}.json",lambda:B.fetch(f"{B.GAMMA}/events?slug={slug}"))
    if not ev: return None
    m=ev[0]["markets"][0]
    if not m.get("closed"): return None
    oc=json.loads(m.get("outcomes") or "[]"); pr=[float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
    tk=json.loads(m.get("clobTokenIds") or "[]")
    if oc[:2]!=["Up","Down"] or len(pr)<2: return None
    w="Up" if pr[0]>=0.99 else ("Down" if pr[1]>=0.99 else None)
    return w and {"slug":slug,"start":start,"up_token":tk[0],"winner":w}
def load_hist(e,dur):
    s=int(e["start"].timestamp())
    def load():
        return B.fetch(f"{B.CLOB}/prices-history?market={e['up_token']}&startTs={s-600}&endTs={s+dur*60+60}&fidelity=1").get("history",[])
    return sorted(B.cached(cache,f"ph_{e['slug']}.json",load),key=lambda p:p["t"])
now=datetime.now(timezone.utc).replace(second=0,microsecond=0)
series={"btc":(60,range(30,58),lambda s:B.load_event(s,cache)),"btc15":(15,range(7,15),load_event15)}
klines=B.load_klines(now-timedelta(days=DAYS,hours=4),now.replace(minute=0),cache)
fee=lambda p:0.07*p*(1-p)
for name,(dur,window,loader) in series.items():
    base=now.replace(minute=(now.minute//dur)*dur) if dur<60 else now.replace(minute=0)
    starts=[base-timedelta(minutes=dur*k) for k in range(2,DAYS*24*60//dur+2)]
    with ThreadPoolExecutor(8) as ex: evs=[e for e in ex.map(loader,starts) if e]
    with ThreadPoolExecutor(8) as ex: hs=list(ex.map(lambda e:load_hist(e,dur),evs))
    rules={"SEKARANG edge>=.05":lambda c:c["edge"]>=.05 and c["ask"]<=.9,
           "edge>=.05 & harga>=.30":lambda c:c["edge"]>=.05 and .30<=c["ask"]<=.9,
           "edge>=.05 & harga>=.40":lambda c:c["edge"]>=.05 and .40<=c["ask"]<=.9,
           "edge>=.05 & harga>=.50":lambda c:c["edge"]>=.05 and .50<=c["ask"]<=.9,
           "edge>=.10":lambda c:c["edge"]>=.10 and c["ask"]<=.9,
           "FAV 0.55-0.85":lambda c:c["fav"] and .55<=c["ask"]<=.85,
           "FAV 0.60-0.90":lambda c:c["fav"] and .60<=c["ask"]<=.90,
           "FAV 0.70-0.90":lambda c:c["fav"] and .70<=c["ask"]<=.90}
    res={r:[] for r in rules}
    for e,h in zip(evs,hs):
        t0=int(e["start"].timestamp()*1000)
        if t0 not in klines: continue
        o=klines[t0][0]; done=set()
        for m in window:
            t=t0+m*60000; last=klines.get(t-60000)
            closes=[klines[k][1] for k in range(t-121*60000,t,60000) if k in klines]
            pu=B.price_after(h,t//1000,60)
            if last is None or len(closes)<60 or pu is None: continue
            s=statistics.pstdev([math.log(b/a) for a,b in zip(closes,closes[1:])]) or 1e-9
            mu=B.phi(math.log(last[1]/o)/(s*math.sqrt(dur-m)))
            cands=[]
            for side,p,mp in(("Up",pu,mu),("Down",1-pu,1-mu)):
                ask=min(.99,p+.01); cands.append({"side":side,"ask":ask,"edge":mp-ask-fee(ask),"fav":p>=.5})
            for r,f in rules.items():
                if r in done: continue
                ok=[c for c in cands if f(c)]
                if ok:
                    c=max(ok,key=lambda c:c["edge"]); done.add(r)
                    res[r].append((c["ask"]+fee(c["ask"]),e["winner"]==c["side"],e["start"]))
    print(f"\n=== {name}: {len(evs)} market resolved, {DAYS} hari ===")
    print(f"{'aturan':26} {'n':>5} {'WR':>5} {'harga':>6} {'ROI':>7} {'ROI 1/2':>8} {'ROI 2/2':>8}")
    mid=now-timedelta(days=DAYS/2)
    for r,lst in res.items():
        if not lst: continue
        roi=lambda L:(sum(w for _,w,_ in L)/sum(c for c,_,_ in L)-1) if L else float('nan')
        a=[x for x in lst if x[2]<mid]; b=[x for x in lst if x[2]>=mid]
        print(f"{r:26} {len(lst):>5} {sum(w for _,w,_ in lst)/len(lst):>5.0%} {sum(c for c,_,_ in lst)/len(lst):>6.2f} {roi(lst):>+7.1%} {roi(a):>+8.1%} {roi(b):>+8.1%}")
