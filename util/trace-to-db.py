#!/usr/bin/python3
#
# This script consumes a trace on stdin and writes, for each allocated
# object, a row to the 'allocs' table containing the allocated size, call
# stack, and {alloc,realloc,free} timestamps.  For each reallocation event,
# a similar row is inserted into the 'reallocs' table.

import argparse
import logging
import os
import sqlite3
import sys

if __name__ == "__main__" and __package__ is None:
    import os
    sys.path.append(os.path.dirname(sys.path[0]))

from common.run import Run

class MetadataTracker() :
    def __init__(self, tslam, ia, ir) :
        self._nextoid = 1
        self._tva2oid = {}  # OIDs
        self._oid2amd = {}  # Allocation metadata (stack, timestamp, size)
        self._oid2irt = {}  # Initial Reallocation Timestamp
        self._oid2rmd = {}  # most recent Reallocation metadata (stk, ts, osz, nsz)
        self._tslam   = tslam
        self._ia      = ia  # Insert Allocation   (on free)
        self._ir      = ir  # Insert Reallocation (on free or realloc)

    def _allocd(self, stk, tva, sz, now):
        oid = self._nextoid
        self._nextoid += 1
        self._tva2oid[tva] = oid
        self._oid2amd[oid] = (stk, now, sz)
        return oid

    def allocd(self, stk, begin, end) :
        now = self._tslam()

        if self._tva2oid.get(begin, None) is not None:
            # synthesize free so we don't lose the object; its lifetime
            # will be a little longer than it should be...
            logging.warn("malloc inserting free for tva=%x at ts=%d", begin, now)
            self.freed("", begin)

        oid = self._allocd(stk, begin, end-begin, now)
        self._oid2rmd[oid]   = None

    def freed(self, _, begin) :
        now = self._tslam()

        oid = self._tva2oid.pop(begin, None)
        if oid is None :
            if begin != 0 :
                # Warn, but do not insert anything into the database
                logging.warn("Free of non-allocated object; tva=%x ts=%d",
                             begin, now)
            return

        irt = self._oid2irt.pop(oid, None)
        self._ia(oid, self._oid2amd.pop(oid), irt, now)

        rmd = self._oid2rmd.pop(oid, None)
        if rmd is not None: self._ir(oid, rmd, now)

    def reallocd(self, stk, otva, ntva, etva) :
        now = self._tslam()
        nsz = etva - ntva

        if self._tva2oid.get(ntva, None) is not None :
            # Synthesize a free before we clobber something
            logging.warn("realloc inserting free for tva=%x at ts=%d", ntva, now)
            self.freed("", ntva)

        oid = self._tva2oid.pop(otva, None)
        if oid is None :
            # allocation via realloc or damaged trace
            oid = self._allocd(stk, ntva, nsz, now)
            self._oid2irt[oid] = now
            self._oid2rmd[oid] = (stk, now, 0, nsz)
        elif etva == ntva :
            # free via realloc
            self.freed(None, otva)
        else :
            rmd = self._oid2rmd.pop(oid, None)
            osz = None
            if rmd is not None :
                self._ir(oid, rmd, now)
                osz = rmd[3]
            else :
                osz = self._oid2amd[oid][2]
                self._oid2irt[oid] = now
            self._oid2rmd[oid] = (stk, now, osz, nsz)
            self._tva2oid[ntva] = oid

    def finish(self) :
      # Record the never-freed objects
      self._tslam = lambda : None

      # Can't just iterate the keys, because free deletes.  So just repeatedly
      # restart a fresh iterator.
      while self._tva2oid != {} :
        k = next(iter(self._tva2oid.keys()))
        self.freed("", k)

if __name__ == "__main__" and __package__ is None:

  argp = argparse.ArgumentParser(description='Generate a database of allocation metadata')
  argp.add_argument('database', action='store', help="Output database")
  argp.add_argument("--log-level", help="Set the logging level",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                    default="INFO")
  args = argp.parse_args()

  logging.basicConfig(level=logging.getLevelName(args.log_level))

  if os.path.isfile(args.database) :
    print("Refusing to clobber existing file", file=sys.stderr)
    sys.exit(-1)

  con = sqlite3.connect(args.database)
  con.execute("CREATE TABLE allocs "
              "(oid INTEGER PRIMARY KEY NOT NULL"
              ", sz INTEGER NOT NULL"
              ", stk TEXT NOT NULL"
              ", ats INTEGER NOT NULL"
              ", rts INTEGER"
              ", fts INTEGER)")
  con.execute("CREATE TABLE reallocs "
              "(oid INTEGER NOT NULL"
              ", osz INTEGER NOT NULL"
              ", nsz INTEGER NOT NULL"
              ", stk TEXT NOT NULL"
              ", ats INTEGER NOT NULL"
              ", fts INTEGER)")

  def ia(oid, amd, irt, fts) :
    (astk, ats, asz) = amd
    con.execute("INSERT INTO allocs "
                "(oid,sz,stk,ats,rts,fts) VALUES (?,?,?,?,?,?)",
                (oid,asz,astk,ats,irt,fts)
    )

  def ir(oid, rmd, fts) :
    (rstk, rts, rosz, rnsz) = rmd
    con.execute("INSERT INTO reallocs "
                "(oid,osz,nsz,stk,ats,fts) VALUES (?,?,?,?,?,?)",
                (oid,rosz,rnsz,rstk,rts,fts)
    )


  run = Run(sys.stdin)
  tslam = lambda : run.timestamp_ns
  at = MetadataTracker(tslam, ia, ir)
  run._trace_listeners += [ at ]
  run.replay()

  # Mark in database as never freed.  Could also leave the _tslam adjustment
  # out for a "free on exit" kind of thing.
  # at._tslam = lambda : None
  at.finish()

  con.commit()
  con.close()
