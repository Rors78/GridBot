import pandas as pd, numpy as np, sys
sys.dont_write_bytecode=True; sys.path.insert(0,'.')
from wf import load,to30,PAIRS
f=pd.read_pickle("f.pkl"); f["dn"]=(f.side=="down")&(f.pierce_h<=72)
D={p:to30(load(p)).set_index("b").c for p in PAIRS}
rows=[]
for t,gg in f.groupby("t"):
    if len(gg)<2: continue
    cols={p:D[p][(D[p].index<t)&(D[p].index>=t-180*1800)] for p in gg.pair}
    m=pd.DataFrame(cols).dropna()
    if len(m)<100: continue
    cl=m.corr(); cr=np.log(m).diff().corr()
    v=dict(zip(gg.pair,gg.dn)); ps=list(gg.pair)
    for i in range(len(ps)):
        for j in range(i+1,len(ps)):
            a,b=ps[i],ps[j]; rows.append((abs(cl.loc[a,b]),cr.loc[a,b],v[a],v[b]))
d=pd.DataFrame(rows,columns="lv rt a b".split())
for col in ("lv","rt"):
    d["bk"]=pd.cut(d[col],[-1,0.5,0.72,1.01])
    g=d.groupby("bk",observed=True).apply(lambda x: pd.Series({"n":len(x),"P(B|A)":(x.a&x.b).sum()/max(1,x.a.sum()),"P(both)":(x.a&x.b).mean()}))
    print(col); print(g.round(3))
