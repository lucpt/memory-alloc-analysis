#!/usr/bin/env python3
# Copyright (c) 2018  Lucian Paul-Trifu
# All rights reserved.
#
# This software was developed by SRI International and the University of
# Cambridge Computer Laboratory under DARPA/AFRL contract (FA8750-10-C-0237)
# ("CTSRD"), as part of the DARPA CRASH research programme.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.

from collections import namedtuple
from enum import Enum, unique
import sys
import numpy
import logging
import argparse
import itertools

# https://pypi.org/project/bintrees
from intervaltree import Interval, IntervalTree

if __name__ == "__main__" and __package__ is None:
    import os
    sys.path.append(os.path.dirname(sys.path[0]))

from common.intervalmap import IntervalMap
from common.misc import Publisher
from common.run import Run

@unique
class AddrIvalState(Enum):
    ALLOCD  = 1
    FREED   = 2
    REVOKED = 3

    MAPD    = 4
    UNMAPD  = 5

    __repr__ = Enum.__str__


class AddrIval(Interval):
    __slots__ = ()

    def __new__(cls, begin, end, state):
        return super().__new__(cls, begin, end, state)

    @property
    def state(self):
        return self.data
    # Required for compatibility with the IntervalMap
    @property
    def value(self):
        return self.state

    def __repr__(self):
        r = super().__repr__()
        r = r.replace('Interval', __class__.__name__, 1)
        r = r.replace(str(self.begin), hex(self.begin)[2:])
        r = r.replace(str(self.end), hex(self.end)[2:])
        return r

    __str__ = __repr__


def intervaltree_query_checked(tree, point):
    ival = tree[point]
    assert len(ival) <= 1, 'Bug: overlapping address intervals at {0:x} {1}'.format(point, tree)
    return ival.pop() if ival else None


# XXX-LPT: Should be internalised by AllocatorAddrSpaceModel, and use the backing intervalmap
# at least for the coalesce-with-self part of coalescing
def intervaltree_query_coalesced(tree, point, **kwds):
    ival = intervaltree_query_checked(tree, point)
    if not ival:
        return None
    data_coalesced = {ival.data, }
    data_coalesced.update(kwds.get('coalesce_with', set()))

    ival_left = ival
    while ival_left and ival_left.data in data_coalesced:
        begin = ival_left.begin
        ival_left = intervaltree_query_checked(tree, begin - 1)

    ival_right = ival
    while ival_right and ival_right.data in data_coalesced:
        end = ival_right.end
        ival_right = intervaltree_query_checked(tree, end)

    return AddrIval(begin, end, ival.data)




class BaseAddrSpaceModel:
    def __init__(self, **kwds):
        super().__init__()
        self.size = 0
        self.sweep_size = 0
        self.mapd_size = 0
        self.allocd_size = 0

    @property
    def size_kb(self):
        return self.size // 2**10
    @property
    def size_mb(self):
        return self.size // 2**20

    @property
    def sweep_size_kb(self):
        return self.sweep_size // 2**10
    @property
    def sweep_size_mb(self):
        return self.sweep_size // 2**20

    @property
    def mapd_size_kb(self):
        return self.mapd_size // 2**10
    @property
    def mapd_size_mb(self):
        return self.mapd_size // 2**20

    def size_measured(self, size):
        self.size = size

    def sweep_size_measured(self, sweep_size):
        self.sweep_size = sweep_size

class BaseIntervalAddrSpaceModel(BaseAddrSpaceModel):

    def __init__(self, **kwds):
        super().__init__()
        self.__addr_ivals = IntervalMap.from_valued_interval_domain(AddrIval(0, 2**64, None))

    def _update(self, ival):
        overlaps = self.__addr_ivals[ival.begin-1 : ival.end+1]
        # XXX-LPT: use __init__ kwds.get('calc_amount_for_addr_ival_states', default=[])
        mapd_size_old = sum(((i.end - i.begin) for i in overlaps if i.state is AddrIvalState.MAPD))
        allocd_size_old = sum(((i.end - i.begin) for i in overlaps if i.state is AddrIvalState.ALLOCD))
        self.__addr_ivals.add(ival)
        overlaps = self.__addr_ivals[ival.begin-1 : ival.end+1]
        mapd_size_new = sum(((i.end - i.begin) for i in overlaps if i.state is AddrIvalState.MAPD))
        allocd_size_new = sum(((i.end - i.begin) for i in overlaps if i.state is AddrIvalState.ALLOCD))

        self.mapd_size += mapd_size_new - mapd_size_old
        self.allocd_size += allocd_size_new - allocd_size_old

        output.update()
        #logger.info('%d\t_update_map with %s\n\tmapd_size_old=%d mapd_size_new=%d self.mapd_size=%d',
        #      run.timestamp, ival, mapd_size_old, mapd_size_new, self.mapd_size)


    def addr_ivals_coalesced_sorted(self, begin=None, end=None):
        if begin is not None and end is not None:
            return [i for i in self.__addr_ivals[begin:end] if i.value is not None]
        else:
            return [i for i in self.__addr_ivals if i.value is not None]

    def addr_ival_coalesced(self, point):
        i = self.__addr_ivals[point]
        return i if i.value is not None else None



class AllocatorAddrSpaceModel(BaseIntervalAddrSpaceModel, Publisher):
    def __init__(self):
        super().__init__()
        # _addr_ivals should be protected (i.e. named __addr_ivals), but external visibility
        # is still needed; see intervaltree_query_coalesced and its usage
        self._addr_ivals = IntervalTree()


    def allocd(self, begin, end):
        interval = AddrIval(begin, end, AddrIvalState.ALLOCD)
        overlaps = self._addr_ivals[begin:end]
        overlaps_allocd = [o for o in overlaps if o.state is AddrIvalState.ALLOCD]
        overlaps_freed = [o for o in overlaps if o.state is AddrIvalState.FREED]
        if overlaps_allocd:
            logger.error('%d\tE: New allocation %s overlaps existing allocations %s, chopping them out',
                 run.timestamp, interval, overlaps_allocd)

            #interval_at_begin = self._addr_ivals[begin]
            #assert len(interval_at_begin) <= 1, 'Bug: overlapping address intervals at {0:x} {1}'
            #                                    .format(begin, interval_at_begin)
            #if interval_at_begin:
            #    interval_at_begin = interval_at_begin.pop()
            #    if interval_at_begin.state is AddrI

        if overlaps_freed:
            self._publish('reused', begin, end)
        if overlaps:
            self._addr_ivals.chop(begin, end)
        super()._update(interval)
        self._addr_ivals.add(interval)


    def reallocd(self, begin_old, begin_new, end_new):
        interval_old = intervaltree_query_checked(self._addr_ivals, begin_old)
        if not interval_old:
            logger.warning('%d\tW: No existing allocation to realloc at %x, doing just alloc',
                  run.timestamp, begin_old)
            self.allocd(begin_new, end_new)
            return
        if interval_old.state is not AddrIvalState.ALLOCD:
            logger.error('%d\tE: Realloc of non-allocated interval %s, assuming it is allocated',
                  run.timestamp, interval_old)

        # Free the old allocation, just the part that does not overlap the new allocation
        interval_new = AddrIval(begin_new, end_new, AddrIvalState.ALLOCD)
        if interval_new.overlaps(interval_old):
            super()._update(AddrIval(interval_old.begin, interval_old.end, AddrIvalState.FREED))
            self._addr_ivals.remove(interval_old)
            if interval_new.lt(interval_old):
                self._addr_ivals.add(AddrIval(interval_new.end, interval_old.end, AddrIvalState.FREED))
            if interval_new.gt(interval_old):
                self._addr_ivals.add(AddrIval(interval_old.begin, interval_new.begin, AddrIvalState.FREED))
        else:
            # XXX use _freed and eliminate spurious W/E reporting
            self.freed(begin_old)

        self.allocd(begin_new, end_new)


    def freed(self, begin):
        interval = intervaltree_query_checked(self._addr_ivals, begin)
        if interval:
            self._addr_ivals.remove(interval)
            if begin != interval.begin or interval.state is not AddrIvalState.ALLOCD:
                logger.warning('%d\tW: Freed(%x) misfrees %s', run.timestamp, begin, interval)
            interval = AddrIval(interval.begin, interval.end, AddrIvalState.FREED)
        else:
            logger.warning('%d\tW: No existing allocation to free at %x, defaulting to one of size 1',
                  run.timestamp, begin)
            interval = AddrIval(begin, begin + 1, AddrIvalState.FREED)
        super()._update(interval)
        self._addr_ivals.add(interval)


    def revoked(self, *bes):
        if not isinstance(bes[0], tuple):
            bes = [(bes[0], bes[1])]
        err_str = ''
        for begin, end in bes:
            ivals_allocd = [ival for ival in self._addr_ivals[begin:end] if ival.state is AddrIvalState.ALLOCD]
            if ivals_allocd:
                err_str += 'Bug: revoking address intervals between {0:x}-{1:x} that are still allocated {2}\n'\
                           .format(begin, end, ivals_allocd)
        assert not err_str, err_str

        for begin, end in bes:
            ival = AddrIval(begin, end, AddrIvalState.REVOKED)
            super()._update(ival)
            self._addr_ivals.chop(ival.begin, ival.end)
            self._addr_ivals.add(ival)

    # Mapped/unmapped by the allocator for its own use
    def mapd(self, callstack, begin, end):
        if any((callstack.find(frame) >= 0 for frame in ('malloc', 'calloc', 'realloc', 'free'))):
            self._update(AddrIval(begin, end, AddrIvalState.MAPD))
    def unmapd(self, callstack, begin, end):
        self._update(AddrIval(begin, end, AddrIvalState.UNMAPD))


    def addr_ivals(self, begin=None, end=None):
        if begin is not None and end is not None:
            return self._addr_ivals[begin:end]
        else:
            return self._addr_ivals[:]

    def addr_ival(self, point):
        return intervaltree_query_checked(self._addr_ivals, point)


class AddrSpaceModel(BaseIntervalAddrSpaceModel):
    def mapd(self, _, begin, end):
        self._update(AddrIval(begin, end, AddrIvalState.MAPD))

    def unmapd(self, _, begin, end):
        self._update(AddrIval(begin, end, AddrIvalState.UNMAPD))

class AccountingAddrSpaceModel(BaseAddrSpaceModel):
    def __init__(self):
        super().__init__()
        self._va2sz = {}

    def mapd(self, _, begin, end):
        self.mapd_size += end - begin
    def unmapd(self, _, begin, end):
        self.mapd_size -= end - begin

    def allocd(self, begin, end):
        sz = end - begin
        self._va2sz[begin] = sz
        self.allocd_size += sz
    def freed(self, begin):
        sz = self._va2sz.get(begin)
        if sz is not None :
            self.allocd_size -= sz
            del self._va2sz[begin]
    def reallocd(self, obegin, nbegin, nend):
        self.freed(obegin)
        self.allocd(nbegin, nend)

class AllocatorAddrSpaceModelSubscriber:
    def reused(self, alloc_state, begin, end):
        raise NotImplemented


class BaseSweepingRevoker(AllocatorAddrSpaceModelSubscriber):
    def __init__(self, capacity_ivals=2**64):
        super().__init__()
        self.swept = 0
        self.sweeps = []
        self._capacity_ivals = int(capacity_ivals)

    @property
    def swept_mb(self):
        return self.swept // 2**20

    @property
    def swept_gb(self):
        return self.swept // 2**30


    def revoked(self, *bes):
        self._sweep(addr_space.size, [AddrIval(b, e, AddrIvalState.FREED) for b, e in bes])


    # XXX-LPT: pack into sweeps of L addr_ivals, where L is an imposed limit
    def _sweep(self, amount, addr_ivals):
        if len(addr_ivals) > self._capacity_ivals:
            raise ValuError('{0} exceeds the limit for intervals at once ({1})'
                            .format(len(addr_ivals), self._capacity_ivals))

        # XXX: can I format AddrIval like [x+mb]
        #ts_str = '{0:>' + str(len(str(run.timestamp))) + '}'
        #if hasattr(self, '_ns_last_print'):
        #    delta_ns = run.timestamp_ns - self._ns_last_print
        #    if delta_ns > 10**9:
        #        delta_str = str(delta_ns // 10**9) + 's'
        #    elif delta_ns > 10**6:
        #        delta_str = str(delta_ns // 10**6) + 'ms'
        #    elif delta_ns > 10**3:
        #        delta_str = str(delta_ns // 10**3) + 'us'
        #    else:
        #        delta_str = str(delta_ns) + 'ns'
        #    ts_str = ts_str.format('+' + delta_str)
        #else:
        #    ts_str = ts_str.format(run.timestamp)
        #print('{0}\tSweep {1:d}MB revoking references to {2} intervals'.format(ts_str, amount // 2**20, addr_ivals),
        #      file=sys.stdout)
        #self._ns_last_print = run.timestamp_ns
        #self.sweeps.append((run.timestamp_ns, amount))

        self.swept += amount
        output.update()


class NaiveSweepingRevoker(BaseSweepingRevoker):
    def reused(self, alloc_state, begin, end):
        intervals = [i for i in alloc_state.addr_ivals(begin, end) if i.state is AddrIvalState.FREED]
        if intervals:
            self._sweep(addr_space.size, intervals)
        for ival in intervals:
            alloc_state.revoked(ival.begin, ival.end)


class CompactingSweepingRevoker(BaseSweepingRevoker):
    def reused(self, alloc_state, begin, end):
        intervals = [i for i in alloc_state.addr_ivals() if i.state is AddrIvalState.FREED]
        intervals.sort(reverse=True)
        intervals_coalesced = []

        while intervals:
            ival = intervaltree_query_coalesced(alloc_state._addr_ivals, intervals.pop().begin,
                                                coalesce_with={AddrIvalState.REVOKED})
            intervals_coalesced.append(ival)
            while intervals and ival.end >= intervals[-1].begin:
                assert ival.end > intervals[-1].begin, '{0} failed to coalesce with {1}'.format(ival, intervals[-1])
                intervals.pop()

        if intervals_coalesced:
            self._sweep(addr_space.size, intervals_coalesced)
        for ival in intervals_coalesced:
            alloc_state.revoked(ival.begin, ival.end)


class BaseOutput:
    def __init__(self, file):
        self._file = file

    def rate_limited_runms(call_period_ms):
        def _rate_limited_ms(meth):
            call_last_ms = 0
            def rate_limited_meth(self, *args):
                nonlocal call_last_ms
                if call_last_ms == 0 or run.timestamp_ms - call_last_ms > call_period_ms:
                    meth(self, *args)
                    call_last_ms = run.timestamp_ms
            return rate_limited_meth
        return _rate_limited_ms

    def update(self):
        raise NotImplementedError


class CompositeOutput(BaseOutput):
    def __init__(self, file, *outputs):
        super().__init__(file)
        self._outputs = outputs

    def update(self):
        for o in self._outputs:
            o.update()


class GraphOutput(BaseOutput):
    def __init__(self, file):
        super().__init__(file)
        self._print_header()

    def _print_header(self):
        print('#{0}\t{1}\t{2}\t{3}\t{4}\t{5}'.format('timestamp', 'addr-space-total', 'addr-space-sweep',
              'allocator-mapped', 'allocator-allocd', 'allocator-swept'), file=self._file)

    @BaseOutput.rate_limited_runms(100)
    def update(self):
        print('{0}\t{1}\t{2}\t{3}\t{4}\t{5}'.format(run.timestamp, addr_space.size, addr_space.sweep_size,
              alloc_state.mapd_size, alloc_state.allocd_size, revoker.swept), file=self._file)


class AllocationMapOutput(BaseOutput, AllocatorAddrSpaceModelSubscriber):
    POOL_MAX_ARTIFICIAL_GROWTH = 0x1000

    POOL_MAP_RESOLUTION_IN_SYMBOLS = 60


    def __init__(self, *args):
        super().__init__(*args)
        self._addr_ivals_reused = IntervalTree()


    def reused(self, alloc_state, begin, end):
        self._addr_ivals_reused.add(AddrIval(begin, end, None))


    # XXX-LPT _print_header
    #def _print_header(self)


    @BaseOutput.rate_limited_runms(100)
    def update(self):
        pools = self._get_memory_pools()

        print('---', file=self._file)
        for p in pools:
            psize = p.end - p.begin

            chunk_size, rem = psize // AllocationMapOutput.POOL_MAP_RESOLUTION_IN_SYMBOLS,\
                              psize % AllocationMapOutput.POOL_MAP_RESOLUTION_IN_SYMBOLS
            chunk_size, rem = (chunk_size, rem) if chunk_size > 0 else (psize, 0)
            chunk_offsets = itertools.chain(range(0, rem * (chunk_size+1), chunk_size+1),
                                            range(rem * (chunk_size+1), psize, chunk_size))
            chunks = [p.begin + co for co in chunk_offsets]
            chunk_states = (self._chunk_state(cb, ce) for cb, ce in
                            itertools.zip_longest(chunks, chunks[1:], fillvalue=p.end));
            chunk_states_str = ''.join(chunk_states)
            print('{0:x}-{1:x} {2:s} {3:d}'.format(p.begin, p.end, chunk_states_str, chunk_size), file=self._file)

        self._addr_ivals_reused.clear()


    @staticmethod
    def _get_memory_pools():
        ivals = [i for i in alloc_state.addr_ivals_coalesced_sorted() if i.state is not AddrIvalState.UNMAPD]
        ivals.reverse()

        pools = []
        while ivals:
            pool = []
            pool.append(ivals.pop())
            while ivals and (ivals[-1].begin - pool[-1].end < AllocationMapOutput.POOL_MAX_ARTIFICIAL_GROWTH):
                assert pool[-1].end <= ivals[-1].begin, 'Bug: overlapping intervals {0} {1}'\
                                                        .format(pool[-1], ivals[-1])
                pool.append(ivals.pop())
            pools.append(AddrIval(pool[0].begin, pool[-1].end, None))

        return pools


    def _chunk_state(self, cbegin, cend):
        #print('{0:x}-{1:x}'.format(cbegin, cend), file=self._file)
        csize = cend - cbegin
        coverlaps = alloc_state.addr_ivals_coalesced_sorted(cbegin, cend)
        if self._addr_ivals_reused.search(cbegin, cend):
            chunk_stat = '~'   # 'reused'  (could be just partially)
        elif all(i.state in (AddrIvalState.MAPD, AddrIvalState.UNMAPD,
                              AddrIvalState.FREED, AddrIvalState.REVOKED) for i in coverlaps):
            chunk_stat = '0'   # 'freed'
        elif all((i.state is AddrIvalState.ALLOCD) for i in coverlaps) and\
             sum((min(i.end, cend) - max(i.begin, cbegin)) for i in coverlaps) >= 0.80 * csize:
            chunk_stat = '#'   # 'allocd'
        else:
            chunk_stat = '='   # 'fragmented'
        return chunk_stat


# Parse command line arguments
argp = argparse.ArgumentParser(description='Model allocation from a trace and output various measurements')
argp.add_argument("--log-level", help="Set the logging level",
                  choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL/default/"],
                  default="CRITICAL")
argp.add_argument("--allocation-map-output", help="Output file for the allocation map (disabled by default)")
argp.add_argument('revoker', choices=["NaiveSweepingRevoker/default/", "CompactingSweepingRevoker", "account"],
                  default='NaiveSweepingRevoker',
                  help="Select the revoker type, or 'account' to assume error-free trace and speed up the"
                  " stats gathering.")
args = argp.parse_args()

# Set up logging
logging.basicConfig(level=logging.getLevelName(args.log_level), format="%(message)s")
logger = logging.getLogger()

# Set up the model
if args.revoker == "account":
    alloc_state = AccountingAddrSpaceModel()
    addr_space = AccountingAddrSpaceModel()
    revoker = BaseSweepingRevoker()
else:
    alloc_state = AllocatorAddrSpaceModel()
    addr_space = AddrSpaceModel()
    revoker_cls = globals()[args.revoker]
    revoker = revoker_cls()
    alloc_state.register_subscriber(revoker)

# Set up the output
output = GraphOutput(sys.stdout)
if args.allocation_map_output:
    map_output_file = open(args.allocation_map_output, 'w')
    alloc_map_output = AllocationMapOutput(map_output_file)
    output = CompositeOutput(None, output, alloc_map_output)
    alloc_state.register_subscriber(alloc_map_output)
run = Run(sys.stdin,
          trace_listeners=[alloc_state, addr_space, revoker],
          addr_space_sample_listeners=[addr_space, ])
run.replay()
output.update()  # ensure at least one line of output
if args.allocation_map_output:
    map_output_file.close()
'''
print('----', file=sys.stdout)
print('{0} swept {1}GB in {2} sweeps in a {3}s run trace\n'
      .format(type(revoker).__name__, revoker.swept_gb, len(revoker.sweeps), run.duration // 10**9), file=sys.stdout)

print('                          Sweep amount per time until next sweep', file=sys.stdout)
sweeps_sweep_per_s = [(s // (t2 - t1)) * 10**9 for (t1, s), (t2, _) in zip(revoker.sweeps, revoker.sweeps[1:]) if t2 - t1 > 0]
bins_gb = (0, 1, 2, 4, 8, 16, 32, 64)
# XXX-LPT do the returned bins match bins_gb?
freqs, bins = numpy.histogram(sweeps_sweep_per_s, bins=[g * 2**30 for g in bins_gb])
for bin_low, bin_high, freq in zip(bins_gb, bins_gb[1:], freqs):
    print('{0:2} - {1:2} GB/s  | {2:<50} {3}'.format(bin_low, bin_high, '=' * int((freq / len(sweeps_sweep_per_s)) * 50),
          freq), file=sys.stdout)

print('                                  Sweep amount histogram', file=sys.stdout)
bins_mb = (0, 64, 128, 256, 512, 1024, 2048, 4096, 8192)
freqs, bins = numpy.histogram(revoker.sweeps, bins=[m * 2**20 for m in bins_mb])
for bin_low, bin_high, freq in zip(bins_mb, bins_mb[1:], freqs):
    print('{0:4} - {1:4} MB  | {2:<50} {3}'.format(bin_low, bin_high, '=' * int((freq / len(revoker.sweeps)) * 50),
          freq), file=sys.stdout)
'''
