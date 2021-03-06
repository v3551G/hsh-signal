from __future__ import division

import numpy as np
from .signal import filter_fft_ff
from .iter import pairwise
import time


class FilterBlock(object):
    """Realtime batch-processing filter block interface."""
    def __init__(self):
        self._consumer = None

    def connect(self, consumer):
        """connect to a consumer"""
        self._consumer = consumer

    def put(self, x):
        """batch-add an array and push results through consumers"""
        assert(self._consumer is not None)
        self._consumer.put(self.batch(x))

    def batch(self, x):
        """batch-process an array and return array of output values"""
        raise NotImplementedError("override me: FilterBlock.batch()")


class SourceBlock(FilterBlock):
    """Filter block providing data from some input hardware device."""
    def batch(self, x):
        return x


class SinkBlock(FilterBlock):
    """Filter block accepting data."""
    def put(self, x):
        raise NotImplementedError("override me: SinkBlock.put()")


class DataSink(SinkBlock):
    """Simply collects the data."""
    def __init__(self, dtype=None):
        super(DataSink, self).__init__()
        self.data = np.array([], dtype=dtype)
        self.dtype = dtype

    def put(self, x):
        self.data = np.concatenate([self.data, x])

    def reset(self):
        self.data = np.array([], dtype=self.dtype)


class Delay(FilterBlock):
    """Simple time delay filter block. Initialized with zeros."""
    def __init__(self, delay):
        """:param delay: delay in number of samples"""
        super(Delay, self).__init__()
        self._buffer = np.zeros(delay)
        self.delay = delay

    def batch(self, x):
        self._buffer = np.concatenate([self._buffer, x])
        y = self._buffer[:-self.delay]
        self._buffer = self._buffer[-self.delay:]
        return y


class PLL(FilterBlock):
    def __init__(self, loop_bw, max_freq, min_freq, sampling_rate):
        """
        Make PLL block that outputs the tracked signal's frequency.

        :param loop_bw:  loop bandwidth, determines the lock range
        :param max_freq: maximum frequency cap (Hz)
        :param min_freq: minimum frequency cap (Hz)
        :param sampling_rate: sampling rate (Hz)
        """
        super(PLL, self).__init__()
        import gr_pll.pll as pll
        self._pll = pll.PLL(loop_bw, max_freq, min_freq, sampling_rate)

    def batch(self, x):
        """batch-process an array and return array of output values (frequency output)"""
        return self._pll.filter_cf(x)

    def batch_vco(self, x):
        """batch-process an array and return VCO output signal"""
        return self._pll.filter_cc(x)


class FIRFilter(FilterBlock):
    """
    Realtime FIR filter.

    Introduces a delay that depends on the filter parameters.
    Also, the first delay output samples are invalid.
    """

    MODE_CONVOLVE = 1
    MODE_FFT_CONVOLVE = 2

    def __init__(self, taps, sampling_rate):
        """
        :param taps:          filter impulse response
        :param sampling_rate: sampling rate (Hz)
        """
        super(FIRFilter, self).__init__()
        self._taps = taps  # filter impulse response
        self.sampling_rate = sampling_rate
        self._ntaps = len(self._taps)
        self._ntaps_front = self._ntaps // 2
        self._ntaps_back = self._ntaps - self._ntaps_front  # for odd ntaps, +1 at the back
        self._buffer_x = np.zeros(self._ntaps - 1)
        self.mode = FIRFilter.MODE_FFT_CONVOLVE

    @property
    def delay(self):
        """Filter delay in number of samples."""
        return self._ntaps_front

    def batch(self, x):
        """batch-process an array and return array of output values"""
        self._buffer_x = np.concatenate([self._buffer_x, x])

        # filter a slightly longer batch, to avoid boundary effects
        #print('len(self._buffer_x)=', len(self._buffer_x), 'len(self._taps)=', len(self._taps))
        #Logger.info(str(('len(self._buffer_x)=', len(self._buffer_x), 'len(self._taps)=', len(self._taps))))
        if self.mode == FIRFilter.MODE_CONVOLVE:
            # slow. for testing only
            filtered = np.convolve(self._buffer_x, self._taps, mode='valid')
            #filtered *= 0.1
        elif self.mode == FIRFilter.MODE_FFT_CONVOLVE:
            filtered = filter_fft_ff(self._buffer_x, self._taps)
        else:
            raise ValueError('invalid FIRFilter mode')

        # trim buffer: just keep trailing bit to include it into leading boundary of next batch
        self._buffer_x = self._buffer_x[-self._ntaps+1:]

        # cut off leading/trailing boundary effect areas
        # (note: introduces a delay of self.iphase)
        #return filtered[self._ntaps_back:-self._ntaps_front]
        return filtered


class HilbertImag(FIRFilter):
    """Part of the Hilbert implementation, turns real part into imaginary part."""
    def __init__(self, ntaps=65):
        """
        :param ntaps   number of taps, made odd if necessary
        """
        ntaps += 1 - ntaps % 2  # ensure taps is odd
        from gr_firdes.firdes import hilbert
        super(HilbertImag, self).__init__(hilbert(ntaps), None)


class Hilbert(FilterBlock):
    """
    Hilbert transform turns a series of real-valued samples into complex-valued (analytic) samples.

    Realtime variant.
    Introduces a delay of ntaps//2 samples -- as opposed to hilbert_fc() which is not realtime.
    Also, the first ntaps//2 output samples are invalid.
    """
    def __init__(self, ntaps=65):
        super(Hilbert, self).__init__()
        self.filter_imag = HilbertImag(ntaps)
        self.filter_real = Delay(self.filter_imag.delay)  # delay so the two parts match up again
        self.delay = self.filter_imag.delay

    def add(self, sample):
        """
        Append a single scalar sample value.
        :returns current filter output value
        """
        #self.put(np.array([sample]))
        raise RuntimeError('not implemented anymore')

    def get(self):
        """:returns current filter output value"""
        raise RuntimeError('not implemented anymore')

    def batch(self, x):
        """batch-process an array and return array of output values"""
        real = self.filter_real.batch(x)
        imag = self.filter_imag.batch(x)
        #print(len(real), len(imag))
        return real + imag * 1j


class Lowpass(FIRFilter):
    """Realtime low-pass filter. Introduces a delay and outputs some initial invalid samples."""
    def __init__(self, cutoff_freq, transition_width, sampling_rate):
        """
        :param cutoff_freq:        beginning of transition band (Hz)
        :param transition_width:   width of transition band (Hz)
        :param sampling_rate:      sampling rate (Hz)
        """
        # design filter impulse response
        from gr_firdes.firdes import low_pass_2
        super(Lowpass, self).__init__(low_pass_2(1, sampling_rate, cutoff_freq, transition_width, 60), sampling_rate)


class Highpass(FIRFilter):
    """Realtime high-pass filter. Introduces a delay and outputs some initial invalid samples."""
    def __init__(self, cutoff_freq, transition_width, sampling_rate):
        """
        :param cutoff_freq:        beginning of transition band (Hz)
        :param transition_width:   width of transition band (Hz)
        :param sampling_rate:      sampling rate (Hz)
        """
        # design filter impulse response
        from gr_firdes.firdes import high_pass_2
        super(Highpass, self).__init__(high_pass_2(1, sampling_rate, cutoff_freq, transition_width, 60), sampling_rate)


class Bandpass(FIRFilter):
    """Realtime band-pass filter. Introduces a delay and outputs some initial invalid samples."""
    def __init__(self, low_cutoff_freq, high_cutoff_freq, transition_width, sampling_rate):
        """
        :param low_cutoff_freq:    center of transition band (Hz)
        :param high_cutoff_freq:   center of transition band (Hz)
        :param transition_width:   width of transition band (Hz)
        :param sampling_rate:      sampling rate (Hz)
        """
        # design filter impulse response
        from gr_firdes.firdes import band_pass_2
        super(Bandpass, self).__init__(band_pass_2(1, sampling_rate, low_cutoff_freq, high_cutoff_freq, transition_width, 60), sampling_rate)


class Bandreject(FIRFilter):
    """Realtime band-reject filter. Introduces a delay and outputs some initial invalid samples."""
    def __init__(self, low_cutoff_freq, high_cutoff_freq, transition_width, sampling_rate):
        """
        :param low_cutoff_freq:    center of transition band (Hz)
        :param high_cutoff_freq:   center of transition band (Hz)
        :param transition_width:   width of transition band (Hz)
        :param sampling_rate:      sampling rate (Hz)
        """
        # design filter impulse response
        from gr_firdes.firdes import band_reject_2
        super(Bandreject, self).__init__(band_reject_2(1, sampling_rate, low_cutoff_freq, high_cutoff_freq, transition_width, 60), sampling_rate)

    def batch(self, x):
        before = time.time()
        res = super(Bandreject, self).batch(x)
        after = time.time()
        #Logger.debug('Bandreject: batch() took {} sec'.format(after-before))
        return res


class Splitter(object):
    def __init__(self):
        self._consumers = []

    def connect(self, consumer):
        """add a consumer"""
        self._consumers.append(consumer)

    def put(self, x):
        """batch-put array to consumers"""
        for consumer in self._consumers:
            consumer.put(x)


class Downsampler(FilterBlock):
    """Evenly spaced downsampler by an integer ratio."""
    def __init__(self, ratio):
        super(Downsampler, self).__init__()
        self.ratio = ratio
        self.waitfor = 0
    def batch(self, x):
        if len(x) <= self.waitfor:
            self.waitfor -= len(x)
            return np.array([])
        ret = x[self.waitfor + np.arange(0, len(x)-self.waitfor, self.ratio)]
        self.waitfor = self.ratio - len(x) % self.ratio
        # TODO: bugged somehow when used through apply_filter()
        # TODO: waitfor %= self.ratio
        return ret

# d=Downsampler(2)
# d.batch(np.array([1]))
# Out[58]:
# array([1])
# In [59]:
#
# d.batch(np.array([2]))
# d.batch(np.array([2]))
# Out[59]:
# array([], dtype=float64)
# In [60]:
#
# d.batch(np.array([3,4,5,6,7]))
# d.batch(np.array([3,4,5,6,7]))
# Out[60]:
# array([3, 5, 7])
# In [61]:
#
# 9
# d.batch(np.array([8,9]))
# Out[61]:
# array([9])


class RegroupBatches(FilterBlock):
    """Splits up large incoming batches into smaller chunks."""
    def __init__(self, out_batch_size):
        super(RegroupBatches, self).__init__()
        self.out_batch_size = out_batch_size

    def batch(self, x):
        return x

    def put(self, x):
        assert(self._consumer is not None)
        starts = np.arange(0, len(x), self.out_batch_size)
        for s in starts:
            self._consumer.put(self.batch(x[s:s+self.out_batch_size]))


class MixLocalOscillator(FilterBlock):
    """Local oscillator and mixer that mixes its output in."""
    def __init__(self, fps, f0):
        super(MixLocalOscillator, self).__init__()
        self.fps, self.f0 = fps, f0
        self.t = 0.0

    def batch(self, x):
        t = self.t + np.arange(len(x))/float(self.fps)
        self.t = t[-1] + 1.0/float(self.fps)
        carrier = np.sin(2*np.pi*self.f0*t)
        return x * carrier


class ChunkDataSource(SourceBlock):
    """
    Fake Microphone signal source for testing. Provides a wav as audio.
    """
    def __init__(self, data, batch_size, sampling_rate=44100):
        super(ChunkDataSource, self).__init__()
        self.sampling_rate = sampling_rate
        self._data = data
        self._batch_size = batch_size
        self._i = 0

    def poll(self):
        """Call this regulary in order to trigger the callback."""
        # currently called with 30 fps in kivy -> could compute batch_size via sampling_rate
        #Logger.debug('FakeMic.poll()')
        before = time.time()
        self.put(self._data[self._i:self._i+self._batch_size])
        after = time.time()
        #Logger.debug('FakeMic: poll() took {} sec'.format(after-before))
        self._i += self._batch_size

    def progress(self):
        return float(self._i) / len(self._data) * 100.0

    def start(self): pass
    def stop(self): pass

    def finished(self):
        return self._i >= len(self._data)


def apply_filter(signal, filter, debug=False):
    signal_padded = np.pad(signal, (0, filter.delay), mode='constant')  # pad with trailing zeros to force returning complete ECG
    source = ChunkDataSource(data=signal_padded, batch_size=179200, sampling_rate=filter.sampling_rate)
    sink = DataSink()
    connect(source, filter, sink)

    # push through all the data
    prev_t = time.time()
    source.start()
    while not source.finished():
        if time.time() > prev_t + 1.0 and debug:
            print 'progress: {} %'.format(source.progress())
            prev_t = time.time()
        source.poll()
    source.stop()

    return sink.data[filter.delay:]  # cut off leading filter delay (contains nonsense output)


def connect(*args):
    """Connect several FilterBlock objects in a chain."""
    for a, b in pairwise(args):
        a.connect(b)

