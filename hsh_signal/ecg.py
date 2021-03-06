import sys
import numpy as np
from signal import localmax_climb, slices, cross_corr, hz2bpm
import matplotlib.pyplot as plt
from .heartseries import Series, HeartSeries
from sklearn.linear_model import TheilSenRegressor


from ishneholterlib import Holter
from heartseries import Series

from quality import sqi_slices, sig_pad
from envelope import envelopes_perc_threshold, envelopes_at_perc
from envelope import beat_penalty_threshold, beat_penalty

from signal import cross_corr


class NoisyECG(object):
    """TODO: this class should not leak out anywhere. Rename and fix external things that break. Then, redesign scrub_ecg()"""
    GOOD_BEAT_THRESHOLD = 0.5  #: normalized cross-correlation threshold for good beats, when compared vs. the median

    def __init__(self, ecg, debug=False):
        """:param ecg: heartseries.Series"""

        # needs package ecg-beat-detector
        # imported here, to avoid loading the big matrices (~ 2 sec) otherwise
        from kimqrsdetector.kimqrsdetector import QRSdetection
        self.QRSdetection = QRSdetection

        self.ecg = ecg
        self.median_ibi = 0.0  #: median inter-beat interval in secs
        self.good_beat_fraction = 0.0  #: fraction of Kim-detected beats which correlate well with the median beat
        self.beat_idxs, self.beat_times = self.beat_detect(debug=debug)

    def is_valid(self, debug=False):
        bpm = hz2bpm(1.0 / (self.median_ibi + 1e-6))
        bpm_ok = bpm >= 30.0 and bpm <= 150.0
        enough_beats = len(self.beat_idxs) >= 5  # at least 5 good beats
        beat_hist_ok = self.good_beat_fraction > 0.5  # meh. not good enough??

        if debug:
            print 'median_ibi={:.3f} -> bpm={:.1f}. len(beat_idxs)={} good_beat_fraction={:.2f}'.format(self.median_ibi, bpm, len(self.beat_idxs), self.good_beat_fraction)
        return bpm_ok and enough_beats and beat_hist_ok

    def slice_good(self, sl, median_beat):
        spectrum = np.abs(np.fft.fft(sl)**2)

        # around 1/8, there is a bottom in a clean signal (see plot of mean beat spectrum)
        lf_hf_db = 10.0 * np.log10(np.sum(spectrum[0:len(spectrum)//8]) / np.sum(spectrum[len(spectrum)//8:len(spectrum)//2]))

        # the slice has similar power like the median_beat
        power_ratio_db = 10.0 * np.log10(np.sum(sl**2) / np.sum(median_beat**2))

        if False:
            plt.plot(spectrum, c='r')
            plt.plot(median_beat, c='k')
            plt.plot(sl, c='b')
            plt.title('slice_good() lf_hf={:.1f} dB  slice/median power_ratio_db={:.1f} dB'.format(lf_hf_db, power_ratio_db))
            plt.show()

        # the slice has similar power like the median_beat
        power_similar = -6.0 < power_ratio_db < 6.0  # 10 dB is a bit lenient. 6 dB would be better, but some baseline drift is larger.

        return lf_hf_db > 5.0 and power_similar

    def beat_detect(self, debug=False, outlierthreshold=0.001):
        # Heuristics:
        # * found enough beats
        # * median ibi is in plausible range (or even: some percentile of ibis is in plausible range)
        # * histogram of beat correlations is plausible (lots of good correlation)
        #
        # Good beats have positive correlation, e.g. rho > 0.5 with the median beat.

        ecg = self.ecg

        #ecg.x[:int(ecg.fps*5)] *= 0.0  # avoid the terrible swing

        #
        # Kim ECG beat detection
        #
        smoothsignal = np.array(ecg.x)
        
        # kill outliers
        mn, mx = np.min(smoothsignal), np.max(smoothsignal)
        m = min(abs(mn),abs(mx))
        N = 100
        step = m/float(N)
        for i in range(N):
            n = len(np.where(smoothsignal<-m)[0]) + len(np.where(smoothsignal>m)[0])
            if n > outlierthreshold*len(smoothsignal):
                break
            m -= step
        mn, mx = -m, m

        smoothsignal[smoothsignal<mn] = mn
        smoothsignal[smoothsignal>mx] = mx
        smoothsignal[-10:] = 0 # extreme outlier in last few frames
        
        # adjust distribution to the one Kim has optimized for
        smoothsignal = (smoothsignal-np.mean(smoothsignal))/np.std(smoothsignal)*0.148213-0.191034
        loc, beattime = self.QRSdetection(smoothsignal, ecg.fps, ecg.t, ftype=0)
        loc = loc.flatten()

        #
        # check error Kim vs. localmax of R peaks
        #
        new_loc = localmax_climb(ecg.x, loc, hwin=int(0.02*ecg.fps))  # hwin = 20 ms
        peak_errs = (new_loc - loc) / float(ecg.fps)
        #print 'np.mean(peak_errs), np.std(peak_errs)', np.mean(peak_errs), np.std(peak_errs)

        ibis = np.diff(loc / float(ecg.fps))
        median_ibi = np.median(ibis)

        #
        # filter beats by cross-correlation with median beat
        #
        ecg_slices = np.array(slices(ecg.x, loc, hwin=int(np.ceil(median_ibi * ecg.fps))//2))
        # median value from each timepoint (not a single one of any of the beats)
        median_beat = np.median(ecg_slices, axis=0)
        if debug:
            plt.plot(np.arange(len(median_beat))/float(ecg.fps), median_beat)
            plt.title('median ECG beat')
        cross_corrs = [cross_corr(sl, median_beat) for sl in ecg_slices]

        spectrum_ok = np.array([self.slice_good(sl, median_beat) for sl in ecg_slices])
        ccs_ok = np.array(cross_corrs) > NoisyECG.GOOD_BEAT_THRESHOLD

        good_loc_idxs = np.where(ccs_ok & spectrum_ok)[0]
        if debug:
            [plt.plot(np.arange(len(ecg_slices[i]))/float(ecg.fps), ecg_slices[i]) for i in range(1,len(ecg_slices)) if i in good_loc_idxs]
            plt.title('all good ECG beats with rho > {:.2f}'.format(NoisyECG.GOOD_BEAT_THRESHOLD))
            plt.show()

        beat_idxs = loc[good_loc_idxs]
        beat_times = beattime[good_loc_idxs]

        self._beattime, self._cross_corrs = beattime, cross_corrs

        if debug:
            self.debug_plot()

        #self.cross_corrs = np.array(cross_corrs)[good_loc_idxs]
        self.median_ibi = median_ibi
        self.good_beat_fraction = float(len(good_loc_idxs)) / len(cross_corrs)
        return beat_idxs, beat_times

    def debug_plot(self):
        ecg = self.ecg

        beattime, cross_corrs = self._beattime, self._cross_corrs

        fig, ax = plt.subplots(2, sharex=True)

        ecg.plot(ax[0])
        ax[0].scatter(self.beat_times, ecg.x[self.beat_idxs], c='r')

        ax[1].stem(beattime, cross_corrs)

        plt.title('beat correlation with median beat')
        plt.show()


def baseline_energy(ecg):
    """The lowest energy level in dB(1) (should be where ECG signal is)."""
    sll = int(ecg.fps*1.0)  # slice len
    idxs = np.arange(0, len(ecg.x)-sll, sll)
    slices = [ecg.x[i:i+sll] for i in idxs]
    #btt = idxs / float(ecg.fps)
    energies = [10.0*np.log10(np.mean(sl**2)) for sl in slices]
    energies_hist = list(sorted(energies))
    return np.mean(energies_hist[:5])  # at least 5 clean ECG beats should be there, hopefully


def scrub_ecg(ecg_in, THRESHOLD = 8.0):
    """
    return an ecg signal where noisy bits are set to zero

    # nb. scrub_ecg() always kills an already-scrubbed lowpass'd ECG... :/  why?
    """
    #ecg = ecg_in.copy()
    #THRESHOLD = 8.0  # dB above baseline_energy()
    ecg = Series(ecg_in.x, ecg_in.fps, ecg_in.lpad)
    #ecg.x = highpass(ecg.x, fps=ecg.fps, cf=2.0, tw=0.4)
    baseline_db = baseline_energy(ecg)
    hwin = int(ecg.fps*0.5)
    check_centers = np.arange(hwin, len(ecg.x)-hwin+1, int(ecg.fps*0.1))  # more densely spaced than hwin
    verdict = []
    for c in check_centers:
        sl = ecg.x[c-hwin:c+hwin+1]
        energy_db = 10.0*np.log10(np.mean(sl**2))
        verdict.append(energy_db < baseline_db + THRESHOLD)

    good_locs = np.where(verdict)[0]
    #flood_fill_width = int(ecg.fps*0.8)
    flood_fill_width = 5  # cf. check_centers step size
    for i in good_locs:
        for j in range(max(i - flood_fill_width, 0), min(i + flood_fill_width + 1, len(verdict))):
            verdict[j] = True

    for c, v in zip(check_centers, verdict):
        if not v:
            ecg.x[c-hwin:c+hwin+1] *= 0.0  # zero the noisy bits

    #ecg.x = np.clip(ecg.x, np.mean(ecg.x) - 10*np.std(ecg.x), np.mean(ecg.x) + 10*np.std(ecg.x))

    return ecg  #, check_centers, verdict


def beatdet_ecg(ecg_in):
    """
    UNTESTED

    beatdet_ecg(scrub_ecg(ecg))

    @see beatdet_alivecor(signal, fps=48000, lpad_t=0) in hsh_signal.alivecor
    """
    from kimqrsdetector.kimqrsdetector import QRSdetection
    smoothsignal = ecg_in.x
    # adjust distribution to the one Kim has optimized for
    smoothsignal = (smoothsignal-np.mean(smoothsignal))/np.std(smoothsignal)*0.148213-0.191034
    loc, beattime = QRSdetection(smoothsignal, ecg_in.fps, ecg_in.t, ftype=0)
    loc = loc.flatten()
    return HeartSeries(ecg_in.x, loc, fps=ecg_in.fps)


def ecg_snr(raw, fps):
    """SNR of AliveCor ECG in raw audio. For quick (0.5 sec) checking whether audio contains ECG or not."""
    win_size = int(2.0*fps)  # 2sec window
    f1 = float(fps) / float(win_size)  # FFT frequency spacing
    power_ecg, power_noise = 0.0, 0.0
    for sl in range(0,len(raw)-win_size,win_size):
        slf = np.fft.fft(raw[sl:sl+win_size])
        # power in the AliveCor band (18.8 kHz)
        ecg_band = slf[int(18000/f1):int(19600/f1)]
        # compared to high-freq baseline noise
        noise_band = slf[int(16000/f1):int(17600/f1)]
        pe, pn = np.sqrt(np.sum(np.abs(ecg_band)**2)) / len(ecg_band), np.sqrt(np.sum(np.abs(noise_band)**2)) / len(noise_band)
        power_ecg += pe
        power_noise += pn
    # nb. division removes const factor of #windows
    if power_noise == 0.0 and power_ecg == 0.0:
        return -10.0  # nothing? pretend bad SNR
    return 20.0 * np.log10(power_ecg / power_noise)


def fix_ecg_peaks(ecg, plt=None):
    ecg = ecg.copy()

    slopesize = int(ecg.fps / 45.0)

    # climb to maxima, and invert if necessary
    ecgidx = [max(i - slopesize, 0) + np.argmax(ecg.x[max(i - slopesize, 0):min(i + slopesize, len(ecg.x) - 1)]) for i in ecg.ibeats]
    beatheight = np.mean(ecg.x[ecgidx]) - np.mean(ecg.x) # average detected beat amplitude
    negecgidx = [max(i - slopesize, 0) + np.argmin(ecg.x[max(i - slopesize, 0):min(i + slopesize, len(ecg.x) - 1)]) for i in ecg.ibeats]
    negbeatheight = np.mean(ecg.x[negecgidx]) - np.mean(ecg.x)  # average detected beat amplitude in the other direction
    if np.abs(negbeatheight) > np.abs(beatheight): # if the other direction has "higher" peaks, invert signal
        ecg.x *= -1
        ecgidx = negecgidx

    if plt != None:
        plt.plot(ecg.t, ecg.x)
        plt.scatter(ecg.t[ecg.ibeats], ecg.x[ecg.ibeats], 30, 'y')

    window = slopesize / 2
    fixed_indices, fixed_times = [], []
    # loop through and linearly interpolate peak flanks
    for i in ecgidx:
        up_start = i
        while ecg.x[up_start] >= ecg.x[i] and up_start > i - slopesize: # make sure start is in trough, not still on peak / plateau
            up_start -= 1
        up_start -= slopesize
        while ecg.x[up_start + 1] <= ecg.x[up_start] and up_start < i - 1: # climb past noise (need to go up)
            up_start += 1
        up_end = i + 2
        while ecg.x[up_end - 1] >= ecg.x[up_end] and up_end > i + 1: # climb past noise (need to go up)
            up_end -= 1
        upidx = np.arange(up_start, up_end) # indices of upslope

        down_start = i
        down_end = i
        while ecg.x[down_end] >= ecg.x[i] and down_end < i + slopesize: # make sure end is in trough, not still on peak / plateau
            down_end += 1
        down_end += slopesize
        while ecg.x[down_start + 1] >= ecg.x[down_start] or ecg.x[down_start + 2] >= ecg.x[down_start] and down_start < down_end: # climb past noise (need to go down)
            down_start += 1
        while ecg.x[down_end - 1] <= ecg.x[down_end] and down_end > down_start: # climb past noise (need to go down)
            down_end -= 1
        downidx = np.arange(down_start, down_end) # indices of downslope

        if len(ecg.t[upidx]) <= 1 or len(ecg.t[downidx]) <= 1: # one or both flanks missing. just use max
            reali = i
            bestt = ecg.t[i]
        else:
            # interpolate flanks
            model1 = TheilSenRegressor().fit(ecg.t[upidx].reshape(-1, 1), ecg.x[upidx])
            model2 = TheilSenRegressor().fit(ecg.t[downidx].reshape(-1, 1), ecg.x[downidx])
            k1, d1 = model1.coef_[0], model1.intercept_
            k2, d2 = model2.coef_[0], model2.intercept_
            angle1, angle2 = np.arctan(k1), np.arctan(k2)
            if False:
                pass
            else:
                bestt = (d2 - d1) / (k1 - k2) # obtain intersection point (noise robust peak)

                if np.abs(bestt - ecg.t[i]) > slopesize or np.abs(angle1) < 0.1 or np.abs(angle2) < 0.1: # calculated intersection point is very far from max - something went wrong - reset
                    print("fix_ecg_peaks WARNING: fixed beat is very far from actual maximum, or slopes suspiciously unsteep. Taking actual maximum to be safe")
                    i = max(i - slopesize, 0) + np.argmax(ecg.x[max(i - slopesize, 0):min(i + slopesize, len(ecg.x) - 1)])
                    if plt != None:
                        reali = i - window + np.argmin(np.abs(ecg.t[(i - window):(i + window)] - bestt))
                        plt.scatter(bestt, ecg.x[reali], 200, 'y')
                        plt.scatter(ecg.t[i], ecg.x[i], 200, 'g')
                        plt.plot([bestt, ecg.t[i]], [ecg.x[reali], ecg.x[i]], 'r', linewidth=2)

                    reali = i
                    bestt = ecg.t[i]
                else:
                    reali = i - window + np.argmin(np.abs(ecg.t[(i - window):(i + window)] - bestt))

        # store fixed times and indices
        fixed_indices.append(reali)
        fixed_times.append(bestt)

        if plt != None:
            # plot
            plt.plot(ecg.t[upidx], ecg.x[upidx], 'g')
            plt.plot(ecg.t[downidx], ecg.x[downidx], 'm')

            if len(upidx) > 1 and len(downidx) > 1:
                plt.plot(ecg.t[upidx], model1.predict(ecg.t[upidx].reshape(-1, 1)), '--k')
                plt.plot(ecg.t[downidx], model2.predict(ecg.t[downidx].reshape(-1, 1)), '--y')

            plt.scatter(ecg.t[reali], ecg.x[reali], 60, 'r')
            plt.scatter(bestt, ecg.x[reali], 90, 'k')

    ecg.tbeats = np.ravel(fixed_times)
    ecg.ibeats = np.ravel(fixed_indices).astype(int)

    return ecg


def ecg_kept(raw):
    sig0 = raw

    fps = sig0.fps

    # print 'slice.x shape=', sig0.slice(slice(int(0*fps), int(ECG_DURATION*fps))).x.shape
    ###

    ecg = beatdet_ecg(sig0.slice(slice(int(0 * fps), int(ECG_DURATION * fps))))
    ecg.lead = LEADS[ECG_LEAD]

    # scaling for plot
    pcs = np.array([np.percentile(ecg.x, 10), np.percentile(ecg.x, 90)])
    pcs = (pcs - np.mean(pcs)) * 5.0 + np.mean(pcs)

    ###

    slicez = sqi_slices(ecg, method='fixed', slice_front=0.5, slice_back=-0.5)
    L = max([len(sl) for sl in slicez])
    padded_slicez = np.array([sig_pad(sl, L, side='center', mode='constant') for sl in slicez])

    ###

    perc = envelopes_perc_threshold(padded_slicez)
    le, ue = envelopes_at_perc(padded_slicez, perc)
    mb = np.median(padded_slicez, axis=0)

    bpt = beat_penalty_threshold(le, ue, mb)

    bps = np.array([beat_penalty(sl, le, ue, mb) for sl in padded_slicez])

    bp_ok = bps < bpt

    ###

    template = np.median(padded_slicez, axis=0)
    corrs = np.array([cross_corr(sl, template) for sl in padded_slicez])

    CORR_THRESHOLD = 0.8
    corr_ok = corrs > CORR_THRESHOLD

    ###

    # next beat is OK as well?
    bp_ibi_ok = bp_ok & np.roll(bp_ok, -1)
    corr_ibi_ok = corr_ok & np.roll(corr_ok, -1)

    igood = np.where(bp_ibi_ok & corr_ibi_ok)[0]
