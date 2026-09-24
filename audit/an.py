import pickle, pandas as pd, numpy as np, collections
R = pickle.load(open("wf.pkl","rb"))
print("total", len(R)); print(collections.Counter(r["reject"] for r in R).most_common(8))
ev = [r for r in R if r["reject"] is None]
print("evaluated", len(ev), "qualified", sum(r["qualified"] for r in ev))
df = pd.DataFrame([{k:v for k,v in r.items() if k not in ("exit","marks")} for r in ev])
print("levels dist", df.levels.value_counts().to_dict()); print("k dist", df.k.value_counts().to_dict())
print("kind", df.kind.value_counts().to_dict())
print("span% median", df.span_pct.median(), "step% median", df.step_pct.median(), "containment median", df.containment.median())
x = df.dropna(subset=["is_fills_grid"])
print("IN-SAMPLE fills: scanner/grid ratio median", (x.is_fills_scanner/x.is_fills_grid.clip(lower=1)).median(), "n", len(x),
      "scanner mean", x.is_fills_scanner.mean(), "grid mean", x.is_fills_grid.mean())
fw = [r for r in ev if r.get("fw_ok")]
rows=[]
for r in fw:
    e = r["exit"]; m = r["marks"]
    d = dict(pair=r["pair"], t=r["t"], q=r["qualified"], yd=r["yield_day"], ya=r["yield_adj"], fd=r["fills_day"], rtd=r["rt_day"],
             step=r["step_pct"], span=r["span_pct"], k=r["k"], lv=r["levels"])
    d["pierce_h"] = e["hours"] if e else np.nan
    d["side"] = e["side"] if e else None
    d["exit_pnl"] = e["pnl"] if e else np.nan
    d["exit_rt"] = e["rt"] if e else np.nan
    for h in ("24h","72h","240h"):
        if h in m:
            d["rule_"+h] = m[h]["rule_total"]; d["hold_"+h] = m[h]["hold_total"]
            d["rulerf_"+h] = m[h]["rule_fills"]; d["holdrt_"+h]=m[h]["hold_rt"]; d["out_"+h]=m[h]["outside"]
    rows.append(d)
f = pd.DataFrame(rows)
f["q_"] = pd.to_datetime(f.t, unit="s").dt.to_period("Q")
f.to_pickle("f.pkl")
A=166.6667
def summ(g, lab):
    n=len(g)
    p24=(g.pierce_h<=24).mean(); p72=(g.pierce_h<=72).mean(); p240=g.pierce_h.notna().mean()
    dn=g[g.side=="down"]; up=g[g.side=="up"]
    print(f"{lab:14s} n={n:4d} pierce<=24h {p24:.0%} <=72h {p72:.0%} <=240h {p240:.0%} | down n={len(dn)} loss/pierce med {dn.exit_pnl.median()/A*100:+.1f}% mean {dn.exit_pnl.mean()/A*100:+.1f}% p10 {dn.exit_pnl.quantile(.1)/A*100:+.1f}% | up n={len(up)} pnl med {up.exit_pnl.median()/A*100:+.1f}% "
          f"| rule240 mean {g.rule_240h.mean()/A*100:+.2f}% med {g.rule_240h.median()/A*100:+.2f}% | hold240 mean {g.hold_240h.mean()/A*100:+.2f}% med {g.hold_240h.median()/A*100:+.2f}% | rule72 mean {g.rule_72h.mean()/A*100:+.2f}%")
summ(f,"ALL"); summ(f[f.q],"QUALIFIED"); summ(f[~f.q],"unqualified")
for q,g in f.groupby("q_"): summ(g, str(q))
for q,g in f[f.q].groupby("q_"): summ(g, "Q:"+str(q))
for p,g in f.groupby("pair"): summ(g,p)
# prediction quality
from scipy.stats import spearmanr
for h in ("24h","72h","240h"):
    days={"24h":1,"72h":3,"240h":10}[h]
    rr = f["rule_"+h]/A*100/days
    print(h, "spearman yield_adj vs rule pnl%/day", spearmanr(f.ya, rr, nan_policy="omit"), "yield_day", spearmanr(f.yd, rr, nan_policy="omit"))
    print(h, "  qualified only", spearmanr(f[f.q].ya, rr[f.q], nan_policy="omit"))
    print(h, " predicted %/day mean", f.yd.mean(), "realised rule %/day mean", rr.mean(), "median", rr.median())
# fills predicted vs actual over first 72h (before exit)
g=f.dropna(subset=["rulerf_72h"])
pred=g.fd*np.minimum(3, g.pierce_h.fillna(1e9)/24)
print("fills in first 72h: predicted", pred.mean(), "actual", g.rulerf_72h.mean(), "ratio(sum)", pred.sum()/max(1,g.rulerf_72h.sum()))
print("pred RT/day mean", f.rtd.mean(), "actual hold RT per day over 240h", (f.holdrt_240h/10).mean())
# quintiles of yield_adj
f["qn"]=pd.qcut(f.ya,5,labels=False)
print(f.groupby("qn").agg(ya=("ya","mean"),n=("ya","size"),p72=("pierce_h",lambda s:(s<=72).mean()),rule240=("rule_240h",lambda s:s.mean()/A*100),hold240=("hold_240h",lambda s:s.mean()/A*100)))
