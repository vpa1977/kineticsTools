#!/usr/bin/env python3

"""
Tool for detecting DNA base-modifications from kinetic signatures.
"""

import cProfile
import functools
import gc
import itertools
import argparse
import json

import os
import logging
import sys
import multiprocessing
import time
import threading
import numpy as np
import queue
import traceback
from importlib import resources

from pbcommand.common_options import add_debug_option
from pbcommand.cli import get_default_argparser_with_base_opts, pacbio_args_runner
from pbcommand.utils import setup_log
from pbcore.io import AlignmentSet

from kineticsTools.KineticWorker import KineticWorkerProcess
from kineticsTools.ResultWriter import KineticsWriter
from kineticsTools.ipdModel import IpdModel
from kineticsTools import ReferenceUtils, loader

__version__ = "3.0"

log = logging.getLogger(__name__)


class Constants(object):
    PVALUE_DEFAULT = 0.01
    MAX_LENGTH_DEFAULT = int(3e12)


def _getResourcePathSpec():
    try:
        with resources.as_file(resources.files('kineticTools') / 'resources') as path:
            return loader.getResourcePathSpec(path)
    except:
        return loader.getResourcePathSpec(os.path.join(os.path.dirname(__file__), 'resources'))


def _validateResource(func, p):
    """Basic func for validating files, dirs, etc..."""
    if func(p):
        return os.path.abspath(p)
    else:
        raise IOError("Unable to find {p}".format(p=p))


def _validateNoneOrResource(func, p):
    """
    Handle optional values. If a file or dir is explicitly provided, then
    it will validated.
    """
    if p is None:
        return p
    else:
        return _validateResource(func, p)


def validateNoneOrPathSpec(ps):
    """
    Handle optional values. If a pathspec is explicitly provided, then
    it will be validated.
    """
    if ps is None:
        return ps
    pths = []
    for p in ps.split(':'):
        pths.append(_validateResource(os.path.isdir, p))
    if not pths:
        raise ValueError("Empty pathspec!")
    return pths


validateFile = functools.partial(_validateResource, os.path.isfile)
validateDir = functools.partial(_validateResource, os.path.isdir)

validateNoneOrFile = functools.partial(_validateNoneOrResource, os.path.isfile)
validateNoneOrDir = functools.partial(_validateNoneOrResource, os.path.isdir)


def get_parser():
    p = get_default_argparser_with_base_opts(
        version=__version__,
        description=__doc__,
        default_level="INFO")
    p.add_argument("alignment_set", help="BAM or Alignment DataSet")
    p.add_argument("--reference", action="store",
                   required=True,
                   type=validateFile, help="Fasta or Reference DataSet")
    p.add_argument("--gff", action="store", default=None,
                   help="Output GFF file of modified bases")
    p.add_argument("--csv", action="store", default=None,
                   help="Output CSV file out per-nucleotide information")
    p.add_argument("--bigwig", action="store", default=None,
                   help="Output BigWig file encoding IpdRatio for both strands")
    # FIXME use central --nproc option
    p.add_argument('--numWorkers', '-j',
                   dest='numWorkers',
                   default=1,
                   type=int,
                   help='Number of thread to use (-1 uses all logical cpus)')
    # common options
    p.add_argument("--pvalue",
                   type=float,
                   default=Constants.PVALUE_DEFAULT,
                   help="P-value cutoff")
    p.add_argument("--maxLength",
                   type=int,
                   default=Constants.MAX_LENGTH_DEFAULT,
                   help="Maximum number of bases to process per contig")
    p.add_argument(
        "--identify",
        action="store",
        default="m6A,m4C",
        help="Specific modifications to identify (comma-separated " +
        "list).  Currrent options are m6A, m4C, m5C_TET.  Using --control " +
        "overrides this option.")
    _DESC = "In the --identify mode, add --methylFraction to " +\
            "command line to estimate the methylated fraction, along with " +\
            "95%% confidence interval bounds."
    p.add_argument("--methylFraction", action="store_true",
                   help=_DESC)
    p.add_argument('--outfile',
                   dest='outfile',
                   default=None,
                   help='Use this option to generate all possible output files. Argument here is the root filename of the output files.')

    # FIXME: Need to add an extra check for this; it can only be used if
    # --useLDA flag is set.
    p.add_argument('--m5Cgff',
                   dest='m5Cgff',
                   default=None,
                   help='Name of output GFF file containing m5C scores')

    # FIXME: Make sure that this is specified if --useLDA flag is set.
    p.add_argument('--m5Cclassifier',
                   dest='m5Cclassifier',
                   default=None,
                   help='Specify csv file containing a 127 x 2 matrix')

    p.add_argument('--pickle',
                   dest='pickle',
                   default=None,
                   help='Name of output pickle file.')

    p.add_argument('--ms_csv',
                   dest='ms_csv',
                   default=None,
                   help='Multisite detection CSV file.')

    # Calculation options:
    p.add_argument('--control',
                   dest='control',
                   default=None,
                   type=validateNoneOrFile,
                   help='AlignmentSet or mapped BAM file containing a control sample. Tool will perform a case-control analysis')

    # Temporary addition to test LDA for Ca5C detection:
    p.add_argument('--useLDA',
                   action="store_true",
                   dest='useLDA',
                   default=False,
                   help='Set this flag to debug LDA for m5C/Ca5C detection')

    # Parameter options:
    defaultParamsPathSpec = _getResourcePathSpec()
    p.add_argument('--paramsPath',
                   dest='paramsPath',
                   default=defaultParamsPathSpec,
                   type=validateNoneOrPathSpec,
                   help='List of :-delimited directory paths containing in-silico trained models (default is "%s")' % defaultParamsPathSpec)

    # XXX hacky workaround for running tests using obsolete chemistry inputs
    p.add_argument("--useChemistry",
                   dest="useChemistry",
                   default=None,
                   help=argparse.SUPPRESS)

    p.add_argument('--minCoverage',
                   dest='minCoverage',
                   default=3,
                   type=int,
                   help='Minimum coverage required to call a modified base')

    p.add_argument('--maxQueueSize',
                   dest='maxQueueSize',
                   default=20,
                   type=int,
                   help='Max Queue Size')

    p.add_argument('--maxCoverage',
                   dest='maxCoverage',
                   type=int, default=-1,
                   help='Maximum coverage to use at each site')

    p.add_argument('--mapQvThreshold',
                   dest='mapQvThreshold',
                   type=float,
                   default=-1.0)

    p.add_argument('--ipdModel',
                   dest='ipdModel',
                   default=None,
                   type=validateNoneOrFile,
                   help='Alternate synthetic IPD model HDF5 file')

    p.add_argument('--modelIters',
                   dest='modelIters',
                   type=int,
                   default=-1,
                   help='[Internal] Number of GBM model iteration to use')

    p.add_argument('--cap_percentile',
                   dest='cap_percentile',
                   type=float,
                   default=99.0,
                   help='Global IPD percentile to cap IPDs at')

    p.add_argument("--methylMinCov",
                   type=int,
                   dest='methylMinCov',
                   default=10,
                   help="Do not try to estimate methylFraction unless coverage is at least this.")

    p.add_argument("--identifyMinCov",
                   type=int,
                   dest='identifyMinCov',
                   default=5,
                   help="Do not try to identify the modification type unless coverage is at least this.")

    p.add_argument("--maxAlignments",
                   type=int,
                   dest="maxAlignments",
                   default=1500,
                   help="Maximum number of alignments to use for a given window")

    # Computation management options:

    p.add_argument("-w", "--referenceWindow", "--referenceWindows",
                   "--refContigs",  # backwards compatibility
                   type=str,
                   dest='referenceWindowsAsString',
                   default=None,
                   help="The window (or multiple comma-delimited windows) of the reference to " + \
                   "be processed, in the format refGroup[:refStart-refEnd] " + \
                   "(default: entire reference).")

    def slurpWindowFile(fname):
        return ",".join(map(str.strip, open(fname).readlines()))

    p.add_argument("--refContigIndex", type=int, dest='refContigIndex', default=-1,
                   help="For debugging purposes only - rather than enter a reference contig name, simply enter an index")

    p.add_argument("-W", "--referenceWindowsFile",
                   "--refContigsFile",  # backwards compatibility
                   type=slurpWindowFile,
                   dest='referenceWindowsAsString',
                   default=None,
                   help="A file containing reference window designations, one per line")

    p.add_argument("--skipUnrecognizedContigs",
                   type=bool,
                   default=False,
                   help="Whether to skip, or abort, unrecognized contigs in the -w/-W flags")
    # FIXME shouldn't it always do this?
    p.add_argument("--alignmentSetRefWindows",
                   action="store_true",
                   dest="referenceWindowsFromAlignment",
                   help="Use refWindows in dataset")

    # Debugging help options:
    p.add_argument("--profile",
                   action="store_true",
                   dest="doProfiling",
                   default=False,
                   help="Enable Python-level profiling (using cProfile).")

    add_debug_option(p)

    p.add_argument("--seed",
                   action="store",
                   dest="randomSeed",
                   type=int,
                   default=None,
                   help="Random seed (for development and debugging purposes only)")

    p.add_argument("--referenceStride", action="store", type=int,
                   default=1000,
                   help="Size of reference window in internal " +
                   "parallelization.  For testing purposes only.")

    return p


class KineticsToolsRunner(object):
    def __init__(self, args):
        self.args = args
        self.alignments = None

    def start(self):
        self.validateArgs()
        return self.run()

    def getVersion(self):
        return __version__

    def validateArgs(self):
        parser = get_parser()
        if not os.path.exists(self.args.alignment_set):
            parser.error('Input AlignmentSet file provided does not exist')

        # Over-ride --identify if --control was specified
        if self.args.control:
            self.args.identify = ""

        if self.args.useLDA:
            if self.args.m5Cclassifier is None:
                parser.error(
                    'Please specify a folder containing forward.csv and reverse.csv classifiers in --m5Cclassifier.')

        if self.args.m5Cgff:
            if not self.args.useLDA:
                parser.error(
                    'm5Cgff file can only be generated in --useLDA mode.')

        # if self.args.methylFraction and not self.args.identify:
        #    parser.error('Currently, --methylFraction only works when the --identify option is specified.')

    def run(self):

        # Figure out what modifications to identify
        mods = self.args.identify
        modsToCall = []
        if mods:
            items = mods.split(",")

            if 'm6A' in items:
                modsToCall.append('H')

            if 'm4C' in items:
                modsToCall.append('J')

            if 'm5C_TET' in items:
                modsToCall.append('K')

            self.args.identify = True
            self.args.modsToCall = modsToCall

        self.options = self.args
        self.options.cmdLine = " ".join(sys.argv)
        self._workers = []

        # set random seed
        # XXX note that this is *not* guaranteed to yield reproducible results
        # indepenently of the number of processing cores used!
        if self.options.randomSeed is not None:
            np.random.seed(self.options.randomSeed)

        if self.args.doProfiling:
            cProfile.runctx("self._mainLoop()",
                            globals=globals(),
                            locals=locals(),
                            filename="profile.out")

        else:
            try:
                ret = self._mainLoop()
            finally:
                # Be sure to shutdown child processes if we get an exception on
                # the main thread
                for w in self._workers:
                    if w.is_alive():
                        w.terminate()

            return ret

    def _initQueues(self):
        # Work chunks are created by the main thread and put on this queue
        # They will be consumed by KineticWorker threads, stored in
        # self._workers
        self._workQueue = multiprocessing.JoinableQueue(
            self.options.maxQueueSize)

        # Completed chunks are put on this queue by KineticWorker threads
        # They are consumed by the KineticsWriter process
        self._resultsQueue = multiprocessing.JoinableQueue(
            self.options.maxQueueSize)

    def _launchSlaveProcesses(self):
        """
        Launch a group of worker processes (self._workers), the queue
        (self._workQueue) that will be used to send them chunks of
        work, and the queue that will be used to receive back the
        results (self._resultsQueue).

        Additionally, launch the result collector process.
        """
        availableCpus = multiprocessing.cpu_count()
        logging.info("Available CPUs: %d" % (availableCpus,))
        logging.info("Requested worker processes: %d" %
                     (self.options.numWorkers,))

        # Use all CPUs if numWorkers < 1
        if self.options.numWorkers < 1:
            self.options.numWorkers = availableCpus

        # Warn if we make a bad numWorker argument is used
        if self.options.numWorkers > availableCpus:
            logging.warn("More worker processes requested (%d) than CPUs available (%d);"
                         " may result in suboptimal performance."
                         % (self.options.numWorkers, availableCpus))

        self._initQueues()

        # Launch the worker processes
        self._workers = []
        for i in range(self.options.numWorkers):
            p = KineticWorkerProcess(
                self.options,
                self._workQueue,
                self._resultsQueue,
                self.ipdModel,
                sharedAlignmentSet=self.alignments)
            self._workers.append(p)
            p.start()
        logging.info("Launched worker processes.")

        # Launch result collector
        self._resultCollectorProcess = KineticsWriter(
            self.options, self._resultsQueue, self.refInfo, self.ipdModel)
        self._resultCollectorProcess.start()
        logging.info("Launched result collector process.")

        # Spawn a thread that monitors worker threads for crashes
        self.monitoringThread = threading.Thread(target=monitorChildProcesses, args=(
            self._workers + [self._resultCollectorProcess],))
        self.monitoringThread.start()

    def _queueChunksForWindow(self, refWindow):
        """
        Compute the chunk extents and queue up the work for a single reference
        """
        winId = refWindow.refId
        winStart = refWindow.start
        winEnd = refWindow.end
        pass

    def loadReferenceAndModel(self, referencePath, ipdModelFilename):
        assert self.alignments is not None and self.referenceWindows is not None
        # Load the reference contigs - annotated with their refID from the
        # alignments
        logging.info("Loading reference contigs {!r}".format(referencePath))
        contigs = ReferenceUtils.loadReferenceContigs(referencePath,
                                                      alignmentSet=self.alignments, windows=self.referenceWindows)
        self.ipdModel = IpdModel(
            contigs, ipdModelFilename, self.args.modelIters)

    def loadSharedAlignmentSet(self, alignmentFilename):
        """
        Read the input AlignmentSet so the indices can be shared with the
        slaves.  This is also used to pass to ReferenceUtils for setting up
        the ipdModel object.
        """
        logging.info("Reading AlignmentSet: %s" % alignmentFilename)
        logging.info("           reference: %s" % self.args.reference)
        self.alignments = AlignmentSet(alignmentFilename,
                                       referenceFastaFname=self.args.reference)
        # XXX this should ensure that the file(s) get opened, including any
        # .pbi indices - but need to confirm this
        self.refInfo = self.alignments.referenceInfoTable

    def _mainLoop(self):
        """
        Main loop
        First launch the worker and writer processes
        Then we loop over ReferenceGroups in the alignments.  For each contig we will:
        1. Load the sequence into the main memory of the parent process
        3. Chunk up the contig and submit the chunk descriptions to the work queue
        Finally, wait for the writer process to finish.
        """

        # This looks scary but it's not.  Python uses reference
        # counting and has a secondary, optional garbage collector for
        # collecting garbage cycles.  Unfortunately when a cyclic GC
        # happens when a thread is calling cPickle.dumps, the
        # interpreter crashes sometimes.  See Bug 19704.  Since we
        # don't leak garbage cycles, disabling the cyclic GC is
        # essentially harmless.
        # gc.disable()

        self.loadSharedAlignmentSet(self.args.alignment_set)

        # Resolve the windows that will be visited.
        if self.args.referenceWindowsAsString is not None:
            self.referenceWindows = []
            for s in self.args.referenceWindowsAsString.split(","):
                try:
                    win = ReferenceUtils.parseReferenceWindow(
                        s, self.alignments.referenceInfo)
                    self.referenceWindows.append(win)
                except BaseException:
                    if self.args.skipUnrecognizedContigs:
                        continue
                    else:
                        raise Exception("Unrecognized contig!")
        elif self.args.referenceWindowsFromAlignment:
            self.referenceWindows = ReferenceUtils.referenceWindowsFromAlignment(
                self.alignments, self.alignments.referenceInfo)
            refNames = set([rw.refName for rw in self.referenceWindows])
            # limit output to contigs that overlap with reference windows
            self.refInfo = [r for r in self.refInfo if r.Name in refNames]
        else:
            self.referenceWindows = ReferenceUtils.createReferenceWindows(
                self.refInfo)

        # Load reference and IpdModel
        chemName = ReferenceUtils.loadAlignmentChemistry(self.alignments)
        if self.args.useChemistry is not None:
            chemName = self.args.useChemistry
        ipdModelFilename = loader.getIpdModelFilename(
            ipdModel=self.args.ipdModel,
            majorityChem=chemName,
            paramsPath=self.args.paramsPath)
        self.loadReferenceAndModel(self.args.reference, ipdModelFilename)

        # Spawn workers
        self._launchSlaveProcesses()

        logging.info(
            'Generating kinetics summary for [%s]' % self.args.alignment_set)

        #self.referenceMap = self.alignments['/RefGroup'].asDict('RefInfoID', 'ID')
        #self.alnInfo = self.alignments['/AlnInfo'].asRecArray()

        # Main loop -- we loop over ReferenceGroups in the alignments.  For each contig we will:
        # 1. Load the sequence into the main memory of the parent process
        # 2. Fork the workers
        # 3. chunk up the contig and

        self.workChunkCounter = 0

        # Iterate over references
        for window in self.referenceWindows:
            logging.info('Processing window/contig: %s' % (window,))
            for chunk in ReferenceUtils.enumerateChunks(
                    self.args.referenceStride, window):
                self._workQueue.put((self.workChunkCounter, chunk))
                self.workChunkCounter += 1

        # Shutdown worker threads with None sentinels
        for i in range(self.args.numWorkers):
            self._workQueue.put(None)

        for w in self._workers:
            w.join()

        # Join on the result queue and the resultsCollector process.
        # This ensures all the results are written before shutdown.
        self.monitoringThread.join()
        self._resultsQueue.join()
        self._resultCollectorProcess.join()
        logging.info("ipdSummary.py finished. Exiting.")
        self.alignments.close()
        return 0


def monitorChildProcesses(children):
    """
    Monitors child processes: promptly exits if a child is found to
    have exited with a nonzero exit code received; otherwise returns
    zero when all processes exit cleanly (0).

    This approach is portable--catching SIGCHLD doesn't work on
    Windows.
    """
    while True:
        all_exited = all(not p.is_alive() for p in children)
        nonzero_exits = [p.exitcode for p in children if p.exitcode]
        if nonzero_exits:
            exitcode = nonzero_exits[0]
            logging.error(
                "Child process exited with exitcode=%d.  Aborting." % exitcode)

            # Kill all the child processes
            for p in children:
                if p.is_alive():
                    p.terminate()

            os._exit(exitcode)
        elif all_exited:
            return 0
        time.sleep(1)


def args_runner(args):
    kt = KineticsToolsRunner(args)
    return kt.start()


def main(argv=sys.argv, out=sys.stdout):
    setup_log_ = functools.partial(setup_log,
                                   str_formatter='%(asctime)s [%(levelname)s] %(message)s')
    try:
        return pacbio_args_runner(
            argv=argv[1:],
            parser=get_parser(),
            args_runner_func=args_runner,
            alog=logging.getLogger(__name__),
            setup_log_func=setup_log)
    # FIXME is there a more central place to deal with this?
    except Exception as e:
        type, value, tb = sys.exc_info()
        traceback.print_exc(file=sys.stderr)
        # Note: if kt.args.usePdb
        # This won't work. If an exception is raised in parseArgs,
        # then kt.args is not defined yet.
        if '--pdb' in argv:
            try:
                # this has better integration with ipython and is nicer
                # pip install ipdb
                import ipdb
                ipdb.post_mortem(tb)
            except ImportError:
                import pdb
                pdb.post_mortem(tb)
        else:
            # exit non-zero
            raise


if __name__ == "__main__":
    sys.exit(main())
