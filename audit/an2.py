import pickle, pandas as pd, numpy as np, os, sys
sys.dont_write_bytecode=True
sys.path.insert(0,'.'); from wf import load, to30, PAIRS
f=pd.read_pickle("f.pkl"); A=166.6667
R={p:load(p) for p in PAIRS}
def fret(p,t,h):
    d=R[p]; j=np.searchsorted(d.timestamp.values,t)
    s=d.close.values[j:j+h]; o=d.open.values[j]
    return s[-1]/o-1 if len(s) else np.nan
f["ret240"]=[fret(p,t,960) for p,t in zip(f.pair,f.t)]
f["ret72"]=[fret(p,t,288) for p,t in zip(f.pair,f.t)]
print("forward 240h asset return mean %.2f%% median %.2f%%"%(f.ret240.mean()*100,f.ret240.median()*100))
# regress grid pnl on asset return
x=f.dropna(subset=["ret240","rule_240h"])
b=np.polyfit(x.ret240*100, x.rule_240h/A*100,1); print("rule240%% = %.3f*ret%% + %.3f (intercept = grid pnl at zero drift)"%tuple(b))
b=np.polyfit(x.ret240*100, x.hold_240h/A*100,1); print("hold240%% = %.3f*ret%% + %.3f"%tuple(b))
flat=x[x.ret240.abs()<0.03]; print("windows with |240h ret|<3%%: n=%d rule mean %.2f%% hold mean %.2f%%"%(len(flat),flat.rule_240h.mean()/A*100,flat.hold_240h.mean()/A*100))
# down pierce loss vs span
d=f[f.side=="down"]; r=(d.exit_pnl/A*100)/d.span
print("down-pierce loss / span: median %.3f (n=%d); implies at live span 51%%: %.1f%% of alloc"%(r.median(),len(d),r.median()*51))
# rule vs hold on down-pierced windows
print("down-pierced windows: rule240 mean %.2f p10 %.2f | hold240 mean %.2f p10 %.2f"%(d.rule_240h.mean()/A*100,d.rule_240h.quantile(.1)/A*100,d.hold_240h.mean()/A*100,d.hold_240h.quantile(.1)/A*100))
print("all: rule240 p5 %.2f p1 %.2f | hold240 p5 %.2f p1 %.2f"%(f.rule_240h.quantile(.05)/A*100,f.rule_240h.quantile(.01)/A*100,f.hold_240h.quantile(.05)/A*100,f.hold_240h.quantile(.01)/A*100))
print("win rate rule240 %.0f%% hold240 %.0f%%"%((f.rule_240h>0).mean()*100,(f.hold_240h>0).mean()*100))
# time to pierce vs lookback
print("median pierce hours (pierced only) %.0f"%f.pierce_h.median())
# correlation: levels vs returns on 30m, 180 bars, at each eval date
D30={p:to30(R[p]).set_index("b").c for p in PAIRS}
ts=sorted(f.t.unique())[::5]
lv,rt=[],[]
for t in ts:
    cols={}
    for p in PAIRS:
        s=D30[p]; s=s[(s.index<t)&(s.index>=t-180*1800)]
        if len(s)>=150: cols[p]=s
    if len(cols)<4: continue
    m=pd.DataFrame(cols).dropna()
    if len(m)<100: continue
    cl=m.corr().values; cr=np.log(m).diff().corr().values
    iu=np.triu_indices(len(cols),1); lv+=list(np.abs(cl[iu])); rt+=list(cr[iu])
lv=np.array(lv); rt=np.array(rt)
print("pair-corr samples n=%d: |level corr| median %.2f, share>=0.72 %.0f%% | return corr median %.2f, share>=0.72 %.0f%%"%(len(lv),np.median(lv),(lv>=.72).mean()*100,np.median(rt),(rt>=.72).mean()*100))
# co-pierce: same t, both down-pierced within 72h, vs independence
g=f.assign(dn72=(f.side=="down")&(f.pierce_h<=72))
cp=[];ind=[]
for t,gg in g.groupby("t"):
    if len(gg)<2: continue
    v=gg.dn72.values; n=len(v)
    for i in range(n):
        for j in range(i+1,n): cp.append(v[i]&v[j])
pdn=g.dn72.mean()
print("P(down pierce<=72h)=%.2f; P(both of two concurrent grids)=%.2f vs %.2f if independent (n pairs %d)"%(pdn,np.mean(cp),pdn**2,len(cp)))
print("P(B down | A down) = %.2f"%(np.mean(cp)/pdn))
