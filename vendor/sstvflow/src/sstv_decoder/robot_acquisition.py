"""Bounded raw reference evidence for Robot acquisition in noisy audio."""
from collections import deque
import numpy as np


def tone_evidence(history, rate, positions, seconds, tones):
    """Explained power of specified tones, including a DC term in each fit."""
    positions = np.atleast_1d(positions)
    n = np.arange(round(seconds*rate))
    samples = history.read(positions[:,None]+n)
    samples -= samples.mean(axis=1,keepdims=True)
    energy = np.sum(samples*samples,axis=1)
    phase = 2*np.pi*np.asarray(tones)[:,None]*n/rate
    basis = np.stack((np.cos(phase),np.sin(phase)),axis=-1)
    basis -= basis.mean(axis=1,keepdims=True)
    gram = np.einsum('fni,fnj->fij',basis,basis)
    projection = np.einsum('wn,fni->wfi',samples,basis)
    coefficients = np.linalg.solve(gram[None,:,:,:],projection[:,:,:,None])[...,0]
    power = np.sum(coefficients*projection,axis=-1)
    return np.clip(power/np.maximum(energy[:,None],1e-12),0,1), np.sqrt(energy/max(1,len(n)))


def robot72_has_extra_sync(history, rate, start, offset, scale=1.):
    """Reject shorter sync cadences masquerading as a Robot72 video line.

    Supported video channels cannot contain sustained 1200 Hz references.
    Inspect the whole line, rather than assuming the competing mode is Robot36.
    """
    try:
        starts=start+np.arange(.025,.297,.001)*scale*rate
        power,level = tone_evidence(history,rate,starts,.0025*scale,
                                    [1200+offset,1500+offset,2300+offset])
        return bool(np.any((level>.003)&(power[:,0]>.65)&
                           (power[:,0]>np.max(power[:,1:],axis=1)*2)))
    except ValueError:
        return True


def robot_sync_edge(history, rate, predicted, offset, scale=1.):
    """Find the leading reference boundary, even when its trailing tail is long."""
    offsets=np.arange(-.004,.0041,.00025)*rate
    positions=predicted+offsets
    try:
        inside,levels=tone_evidence(history,rate,positions+.001*scale*rate,.005*scale,[1200+offset])
        before,_=tone_evidence(history,rate,positions-.004*scale*rate,.003*scale,[1200+offset])
    except ValueError:
        return None
    scores=inside[:,0]-.8*before[:,0]-.03*abs(offsets/(.004*rate))
    best=int(np.argmax(scores))
    if levels[best]<.003 or inside[best,0]<.35 or scores[best]<.25 or best in (0,len(scores)-1):
        return None
    return float(positions[best]),float(np.clip(.65+.3*scores[best],.7,.95))


class RawRobotAcquisition:
    """Scan four-ms raw references; retain only bounded pulse/cadence state.

    This fallback never validates VIS. Several syncs, channel occupancy and
    independently discriminated chroma markers must agree before acquisition.
    """
    def __init__(self, rate):
        self.rate=rate
        self.step=max(1,round(.001*rate))
        self.next_scan=0
        self.run=[]
        self.points=deque(maxlen=64)
        self.tones=np.arange(1000.,1400.1,50.)
        self.checked={}

    def consume(self, history):
        earliest=history.origin+max(0,history.end-history.capacity)
        if self.next_scan<earliest:
            self.next_scan=float(np.ceil(earliest/self.step)*self.step)
            self.run=[]
            self.points.clear()
            self.checked.clear()
        end=history.latest-round(.004*self.rate)-1
        # Batch the small coherent fits rather than fitting per PCM sample.
        starts=np.arange(self.next_scan,end,self.step)
        if not len(starts):
            return
        self.next_scan=float(starts[-1]+self.step)
        scores,levels=tone_evidence(history,self.rate,starts,.004,self.tones)
        best=np.argmax(scores,axis=1)
        for start,row,index,level in zip(starts,scores,best,levels):
            if row[index]>=.35 and level>.003 and index not in (0,len(self.tones)-1):
                a,b,c=row[index-1:index+2]
                correction=.5*(a-c)/(a-2*b+c) if a-2*b+c<0 else 0.
                frequency=self.tones[index]+50*np.clip(correction,-1,1)
                self.run.append((float(start),float(frequency-1200),float(row[index])))
                if len(self.run)>30:
                    self.run=self.run[-30:]
            elif self.run:
                width=(start-self.run[0][0])/self.rate
                if .004<=width<=.025:
                    # At the admission threshold the four-ms aperture crosses
                    # the leading boundary before its center reaches the tone.
                    position=self.run[0][0]+.0015*self.rate
                    self.points.append((position,float(np.median([v[1] for v in self.run])),
                                        float(np.median([v[2] for v in self.run]))))
                self.run=[]

    def candidate(self, mode, history, protocol_history):
        period=.150 if mode=="Robot36" else .300
        count=4
        ready=[p for p in self.points if p[0]+period*self.rate < protocol_history.latest]
        for last in reversed(ready):
            if last[0]<=self.checked.get(mode,-1):
                break
            self.checked[mode]=last[0]
            chain=[last]
            for _ in range(count-1):
                choices=[p for p in ready if p[0]<chain[0][0] and
                         abs((chain[0][0]-p[0])/self.rate-period)<period*.02 and
                         abs(p[1]-last[1])<55]
                if not choices:
                    break
                chain.insert(0,min(choices,key=lambda p:abs((chain[0][0]-p[0])/self.rate-period)))
            if len(chain)<count:
                continue
            measured=np.diff([p[0] for p in chain])/self.rate
            if np.ptp(measured)>.0035:
                continue
            scale=float(np.mean(measured)/period)
            offset=float(np.median([p[1] for p in chain]))
            # A single timing adjustment must explain every marker in the chain.
            shifts=np.arange(-.003,.0031,.0005)*self.rate
            markers=((.101,.003,1500),) if mode=="Robot36" else ((.151,.003,1500),(.226,.003,2300))
            agreement=np.ones(len(shifts),bool)
            phases=[]
            try:
                for line,(start,_,_) in enumerate(chain):
                    channels=((.020,.070),(.112,.030)) if mode=="Robot36" else ((.025,.110),(.165,.050),(.240,.050))
                    for begin,duration in channels:
                        values=protocol_history.interval(start+begin*scale*self.rate,start+(begin+duration)*scale*self.rate)-offset
                        if np.mean((values>1400)&(values<2400))<.60:
                            agreement[:]=False
                            break
                    if mode=="Robot72" and robot72_has_extra_sync(history,self.rate,start,offset,scale):
                        agreement[:]=False
                    for begin,duration,tone in markers:
                        powers,levels=tone_evidence(history,self.rate,start+shifts+begin*scale*self.rate,
                                                    duration*scale,[1200+offset,1500+offset,1900+offset,2300+offset])
                        if mode=="Robot36":
                            expected=1 if line%2==0 else 3
                            # Both possible first-line parities are retained.
                            phases.append(powers)
                        else:
                            expected=1 if tone==1500 else 3
                            competing=np.max(powers[:,[0,3 if expected==1 else 1]],axis=1)
                            agreement &= (powers[:,expected]>.35)&(powers[:,expected]>competing*1.5)&(levels>.003)
                if mode=="Robot36":
                    parity_agreement=np.zeros(len(shifts),bool)
                    for parity in (0,1):
                        votes=np.zeros(len(shifts),int)
                        for line,powers in enumerate(phases):
                            expected=1 if (line+parity)%2==0 else 3
                            competing=np.max(powers[:,[0,3 if expected==1 else 1]],axis=1)
                            votes += (powers[:,expected]>.30)&(powers[:,expected]>competing*2)
                        parity_agreement |= votes>=3
                    agreement &= parity_agreement
            except ValueError:
                continue
            if np.any(agreement):
                shift=shifts[np.flatnonzero(agreement)[np.argmin(abs(shifts[agreement]))]]
                return [(p[0]+shift,p[1],p[2]) for p in chain],scale,offset
        return None
