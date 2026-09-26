"""Walk-forward: current hindsight queue vs causal online queues.
Run review_experiments.py first (it writes results/.review_cache.pkl).
"""
import pickle,pandas as pd,numpy as np
from collections import defaultdict
from src.config import Config
from src.memory_spikes import _episodes
alerts,faults,metrics=pickle.load(open('results/.review_cache.pkl','rb'))
cfg=Config()
origin=min(a.timestamp for a in alerts); sp=origin+pd.Timedelta(days=7)
ser={s:p.set_index('timestamp').value.sort_index() for s,p in metrics.groupby('series_id')}
def jumps(lo,hi):
    out=[]
    for sid,s in ser.items():
        rise=s-s.rolling('15min',closed='left').median()
        hot=rise[(rise>0.5e9)&(rise.index>=lo)&(rise.index<hi)]
        for ep in _episodes(hot,sid,sid,cfg):
            pts=hot[(hot.index>=ep.start)&(hot.index<=ep.end)]
            ep.confirm=pts.index[1]; ep.rc=float(pts.iloc[:2].max()); out.append(ep)
    return out
fs=defaultdict(list)
for f in faults: fs[f.component].append(f)
def hit(j): return [f for f in fs[j.service] if j.start<=f.end and j.end>=f.start]
end=max(a.timestamp for a in alerts)+pd.Timedelta(seconds=1)
Jw1=jumps(origin,sp)
# tune on week 1: pick thr maximizing (true - false) share, i.e. cost 10*miss + 1*false over jumps
best=None
for thr in np.arange(0.5e9,1.2e9,0.05e9):
    sel=[j for j in Jw1 if j.rc>=thr]
    tp=sum(bool(hit(j)) for j in sel); fp=len(sel)-tp
    miss=sum(bool(hit(j)) for j in Jw1 if j.rc<thr)
    c=10*miss+fp
    if best is None or c<best[0]: best=(c,thr,tp,fp)
print('week-1 tuned thr GB',best[1]/1e9,'tp',best[2],'fp',best[3])
thr=best[1]
def queue(J,k,online=True,thr=0):
    d=defaultdict(list)
    for j in J:
        if j.rc>=thr: d[j.start.normalize()].append(j)
    key=(lambda x:x.confirm) if online else (lambda x:-x.peak_rise)
    return [j for v in d.values() for j in sorted(v,key=key)[:k]]
def rate(shown,F):
    ids={f.fault_id for f in F}
    c={f.fault_id for j in shown for f in hit(j) if f.fault_id in ids}; fl=sum(not hit(j) for j in shown)
    return len(c)/len(F), fl
t=sp; rows=[]
while t<end:
    e=min(t+pd.Timedelta(days=7),end)
    F=[f for f in faults if t<=f.start<e]
    if len(F)<5: break
    J=jumps(t,e)
    a=rate(queue(J,20,False),F)[0]; b=rate(queue(J,20,True),F)[0]; c=rate(queue(J,20,True,thr),F)[0]
    ser_cov=sum(any(((ser[f.component].index>=f.start)&(ser[f.component].index<=f.end))) for f in F)/len(F)
    rows.append((t.date(),len(F),round(a,3),round(b,3),round(c,3),round(ser_cov,3)))
    t=e
df=pd.DataFrame(rows,columns=['week','faults','current_hindsight','online_fifo','online_tuned','faults_with_metric_data'])
print(df.to_string()); print(df.mean(numeric_only=True).round(3).to_string())
