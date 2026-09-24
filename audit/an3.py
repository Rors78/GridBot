import pickle, pandas as pd, numpy as np, sys
sys.dont_write_bytecode=True
sys.path.insert(0,r"D:\GridBot"); sys.path.insert(0,'.')
import oracle, gridbot
from wf import load, to30, PAIRS, pts_of
from multiprocessing import Pool
A=166.6667
CFG=[(3,8,"geo"),(6,8,"geo"),(13,8,"geo"),(6,16,"geo"),(13,16,"geo")]
def run(p):
    d15=load(p); d30=to30(d15); t30=d30.b.values; t15=d15.timestamp.values
    f=pd.read_pickle("f.pkl"); out=[]
    for t in f[f.pair==p].t.values[::2]:
        i=np.searchsorted(t30,t); w=d30.iloc[i-480:i]
        rows=[(0,o,h,l,c) for o,h,l,c in zip(w.o,w.h,w.l,w.c)]
        atr=oracle.wilder_atr_pct(rows); closes=list(w.c)
        center=oracle.median(closes[-max(20,len(closes)//5):])
        j=np.searchsorted(t15,t); fw=d15.iloc[j:j+960]; px=float(fw.open.iloc[0])
        for k,n,kind in CFG:
            lo=center*(1-k*atr/100); hi=center*(1+k*atr/100)
            if lo<=0 or not(lo<px<hi): out.append((p,t,k,n,np.nan,np.nan)); continue
            lv=oracle.make_levels(lo,hi,n,kind)
            if (lv[1]/lv[0]-1)*100 < 0.66: out.append((p,t,k,n,np.nan,np.nan)); continue
            for mode in ("rule","hold"):
                g=gridbot.Grid(p,p,lv,kind,A,0.22,0.38); g.deploy(px,t); kk=0; res=None
                for ts,o,h,l,c in zip(fw.timestamp,fw.open,fw.high,fw.low,fw.close):
                    for q in pts_of(o,h,l,c): kk+=1; g.on_print(q,t+kk*1e-3)
                    if mode=="rule" and (c<lo or c>hi): res=g.close(c,ts,"x")["realised"]; break
                if res is None: res=g.mark(float(fw.close.iloc[-1]))["total"]
                if mode=="rule": r1=res
                else: r2=res
            out.append((p,t,k,n,r1,r2))
    return out
if __name__=="__main__":
    with Pool(14) as pl: o=[x for xs in pl.map(run,PAIRS) for x in xs]
    d=pd.DataFrame(o,columns="pair t k n rule hold".split())
    d.to_pickle("cfg.pkl")
    print(d.groupby(["k","n"]).agg(n_=("rule","count"),rule_mean=("rule",lambda s:s.mean()/A*100),rule_med=("rule",lambda s:s.median()/A*100),rule_p5=("rule",lambda s:s.quantile(.05)/A*100),hold_mean=("hold",lambda s:s.mean()/A*100),hold_p5=("hold",lambda s:s.quantile(.05)/A*100)).round(2))
