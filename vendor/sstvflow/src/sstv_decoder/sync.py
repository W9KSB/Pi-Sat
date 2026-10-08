"""Cadence-constrained matched sync evidence, independent of pixel FM estimates."""
import numpy as np


def fit_reference_offset(history, rate, start, tone, center_offset, seconds=.005):
    """Qualified raw reference fit, including DC; defaults to five milliseconds.

    Fit quality is explained power, not frequency certainty. Callers aggregate
    multiple received references before trusting an offset correction.
    """
    n = np.arange(round(seconds * rate))
    try:
        samples = history.read(start + n)
    except ValueError:
        return None
    energy = np.sum((samples - samples.mean())**2)
    if energy < 1e-10:
        return None
    offsets = center_offset + np.arange(-60., 60.1, 2.)
    phase = 2 * np.pi * (tone + offsets[:,None]) * n[None,:] / rate
    basis = np.stack((np.cos(phase), np.sin(phase), np.ones_like(phase)), axis=-1)
    gram = np.einsum('fni,fnj->fij', basis, basis)
    projection = np.einsum('fni,n->fi', basis, samples)
    coefficients = np.linalg.solve(gram, projection[...,None])[...,0]
    errors = np.sum(samples**2) - np.sum(coefficients * projection, axis=1)
    best = int(np.argmin(errors))
    if best in (0, len(offsets)-1):
        return None
    left, middle, right = errors[best-1:best+2]
    curvature = left - 2*middle + right
    correction = np.clip((left-right) / curvature, -2, 2) if curvature > 0 else 0
    explained = float(np.clip(1 - errors[best]/energy, 0, 1))
    amplitude = float(np.hypot(*coefficients[best,:2]))
    if explained < .9 or amplitude < .003:
        return None
    return float(offsets[best] + correction), explained


def robot_porch_matches(history, rate, start, offset):
    """Validate the central 2 ms porch directly when filtered FM is biased.

    Require a strong 1500 Hz sinusoid, independently of competing sync/leader
    tones. This is additional evidence within an already validated cadence,
    not a standalone acquisition trigger.
    """
    n = np.arange(round(.002 * rate))
    try:
        samples = history.read(start + .0095 * rate + n)
    except ValueError:
        return False
    energy = np.sum((samples - samples.mean())**2)
    if energy < 1e-10:
        return False
    explained = []
    amplitudes = []
    for tone in (1200, 1500, 1900):
        phase = 2 * np.pi * (tone + offset) * n / rate
        basis = np.stack((np.cos(phase), np.sin(phase), np.ones(len(n))), axis=1)
        coefficients = np.linalg.lstsq(basis, samples, rcond=None)[0]
        explained.append(1 - np.sum((samples - basis @ coefficients)**2) / energy)
        amplitudes.append(np.hypot(*coefficients[:2]))
    return bool(explained[1] >= .6 and explained[1] >= 2*max(explained[0], explained[2])
                and amplitudes[1] > .003)


def reference_noise_ratio(history, rate, start, tone, offset, seconds=.005):
    """Unexplained power in a constant-tone reference, over five ms by default."""
    n = np.arange(round(seconds * rate))
    try:
        samples = history.read(start + n)
    except ValueError:
        return None
    phase = 2 * np.pi * (tone + offset) * n / rate
    basis = np.stack((np.cos(phase), np.sin(phase), np.ones(len(n))), axis=1)
    coefficients = np.linalg.lstsq(basis, samples, rcond=None)[0]
    energy = np.sum((samples - samples.mean())**2)
    if energy < 1e-10:
        return None
    return float(np.clip(np.sum((samples - basis @ coefficients)**2) / energy, 0, 1))


def matched_sync(history, rate, predicted, sync_seconds, offset, scale=1.0,
                 *, search_before=.003, search_after=.003):
    """Locate the raw 1200-to-1500 Hz boundary within a bounded cadence gate.

    A continuous-phase sync/porch model fits amplitude, phase and DC together.
    Maximizing full-pulse power biases timing by the waveform phase; locating
    its trailing boundary also works when the first sync merges with VIS stop.
    The short following interval fits within the 572 us Martin porch.
    """
    duration = sync_seconds * scale * rate

    def integrated_sync(history, rate, predicted, sync_seconds, offset, scale=1.0):
        """Search a bounded timing window for protocol sync amid unrelated audio.

        This is used only after mode acquisition. Correlation integrates the full
        known sync interval; it does not smooth the picture-frequency estimator.
        """
        count=round(sync_seconds*scale*rate)
        offsets=np.linspace(-.003,.003,25)*rate
        positions=predicted+offsets
        try:
            samples=history.read(positions[:,None]+np.arange(count))
        except ValueError:
            return None
        samples=samples-samples.mean(axis=1,keepdims=True)
        t=np.arange(count)/rate
        powers=[]
        for tone in (1200,1500,1900):
            basis=np.stack((np.cos(2*np.pi*(tone+offset)*t),np.sin(2*np.pi*(tone+offset)*t)),axis=1)
            projection=samples@basis
            coefficients=np.linalg.solve(basis.T@basis,projection.T).T
            powers.append(np.sum(coefficients**2,axis=1))
        sync,porch,leader=powers
        scores=sync-.75*porch-.3*leader
        best=int(np.argmax(scores))
        amplitude=np.sqrt(sync[best])
        dominance=sync[best]/max(porch[best]+leader[best],1e-12)
        contrast = (scores.max()-scores.min())/max(sync[best], 1e-12)
        if amplitude<.003 or dominance<3 or scores[best]<=0 or contrast<.05 or best in (0,len(scores)-1):
            return None
        a,b,c=scores[best-1:best+2]
        curvature=a-2*b+c
        fraction=np.clip(.5*(a-c)/curvature,-1,1) if curvature<0 else 0
        position=positions[best]+fraction*(positions[1]-positions[0])
        confidence=float(np.clip(.6+.1*np.log10(dominance),.6,.95))
        return float(position),confidence

    def fallback():
        # Longer syncs can supply useful integrated evidence during a weak
        # reception even when the short boundary cannot grant precision.
        # Widened first-image searches must establish their actual boundary.
        if sync_seconds < .009 or search_before != .003 or search_after != .003:
            return None
        found = integrated_sync(history, rate, predicted, sync_seconds, offset, scale)
        if found is None:
            return None
        # Full-pulse score contrast establishes an observed boundary even
        # when noise or a captured sync tail obscures the short porch fit.
        return found

    before = round(.0015 * scale * rate)
    after = max(3, round(.0004 * scale * rate))
    n = np.arange(-before, after, dtype=float)
    # Transmitters integrate each sample's frequency before emitting that
    # sample, so the first porch sample already contains one porch increment.
    phase = 2*np.pi*((1200+offset)*n + 300*np.maximum(n+1, 0))/rate
    basis = np.stack((np.cos(phase), np.sin(phase), np.ones(len(n))), axis=1)
    inverse = np.linalg.pinv(basis)
    reference = 2*np.pi*(1200+offset)*n/rate
    constant = np.stack((np.cos(reference), np.sin(reference), np.ones(len(n))), axis=1)
    constant_inverse = np.linalg.pinv(constant)

    def fit(ends):
        try:
            samples = history.read(ends[:,None]+n)
        except ValueError:
            return None
        coefficients = samples @ inverse.T
        errors = np.sum((samples-coefficients @ basis.T)**2, axis=1)
        energy = np.sum((samples-samples.mean(axis=1, keepdims=True))**2, axis=1)
        return errors/np.maximum(energy, 1e-12), samples, coefficients, energy

    step = .000125 * rate
    nominal_end = predicted + duration
    lower = nominal_end - search_before*rate
    upper = nominal_end + search_after*rate
    ends = np.linspace(lower, upper, max(3, int(np.ceil((upper-lower)/step))+1))
    coarse = fit(ends)
    if coarse is None:
        return fallback()
    best = int(np.argmin(coarse[0]))
    if best in (0, len(ends)-1):
        return fallback()
    ends = np.arange(ends[best]-step, ends[best]+step+.25, .5)
    refined = fit(ends)
    if refined is None:
        return fallback()
    errors, samples, coefficients, energy = refined
    best = int(np.argmin(errors))
    if best in (0, len(ends)-1):
        return fallback()
    constant_coefficients = constant_inverse @ samples[best]
    constant_error = np.sum((samples[best]-constant @ constant_coefficients)**2)
    amplitude = np.hypot(*coefficients[best,:2])
    improvement = constant_error/max(energy[best], 1e-12)-errors[best]
    # Weak long-sync receptions retain their integrated timing evidence;
    # short syncs require 80% explained power to reject interfering carriers.
    precision_error = .02 if sync_seconds >= .009 else .2
    if amplitude < .003 or errors[best] > precision_error or improvement < .004:
        return fallback()
    left, middle, right = errors[best-1:best+2]
    curvature = left-2*middle+right
    fraction = np.clip(.5*(left-right)/curvature, -1, 1) if curvature > 0 else 0
    end = ends[best]+fraction*.5
    confidence = float(np.clip(.95-.5*errors[best], .6, .95))
    position = float(end-duration)
    if sync_seconds >= .009 and search_before == .003 and search_after == .003:
        integrated = integrated_sync(history, rate, predicted, sync_seconds, offset, scale)
        # A local tone transition can fit perfectly somewhere within a long
        # pulse gate. It must also agree with the complete expected sync.
        if integrated is None or abs(position-integrated[0]) > .0005*rate:
            return fallback()
    return position, confidence


def robot_chroma_phase(history,rate,start,offset,scale):
    """Use the 4.5 ms reference tone rather than noisy picture FM for parity."""
    count=round(.0035*scale*rate)
    positions=start+.1005*rate*scale+np.arange(count)
    try:
        samples=history.read(positions)
    except ValueError:
        return None
    samples-=samples.mean()
    t=np.arange(count)/rate
    powers=[]
    for tone in (1500,2300):
        basis=np.stack((np.cos(2*np.pi*(tone+offset)*t),np.sin(2*np.pi*(tone+offset)*t)),axis=1)
        coefficients=np.linalg.solve(basis.T@basis,basis.T@samples)
        powers.append(float(np.sum(coefficients**2)))
    if max(powers)<.003**2 or max(powers)<2*min(powers):
        return None
    return "Cr" if powers[0]>powers[1] else "Cb"
