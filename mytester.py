""" runtest.py - run VMs as needed to run a lustre test set
"""
import sys
import os
import fcntl
import time
import threading
import logging
import Queue
import subprocess32
import shlex
import json
import Queue
import shutil
from pprint import pprint
from subprocess32 import Popen, PIPE, TimeoutExpired


class Node(object):
    def __init__(self, name):
        self.name = name # Node name
        self.process = None # Popen object
        self.outs = '' # full accumulated stdout output
        self.errs = '' # full accumulated stderr output

    def is_alive(self):
        if self.process is not None:
            self.process.poll()
            if self.process.returncode is not None:
                return False
        return False

    def wait_for_login(self):
        """ Returns error as string or None if all is fine. No timeout handling """

        fd = self.process.stdout.fileno()
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        while True:
            try:
                string = self.process.stdout.read()
            except:
                string = ''
                time.sleep(1)
            else:
                self.outs += string
                #pprint(string)

            self.process.poll()
            if self.process.returncode is not None:
                # Capture stderr too
                string = self.process.stderr.read()
                self.errs += string
                return "Process died"
            if "login:" in string:
                # Restore old blocking behavior
                fcntl.fcntl(fd, fcntl.F_SETFL, fl)
                return None
        # Hm, the loop ended somehow?
        self.process.poll()
        return "terminated"

    def terminate(self):
        if self.process is None or self.process.returncode is not None:
            return
        try:
            self.process.terminate()
        except OSError: # Already dead? ignore
            pass
        outs, errs = self.process.communicate()
        self.outs += outs
        self.errs += errs

    def returncode(self):
        self.process.poll()
        return self.process.returncode

    def check_node_alive(self):
        self.process.poll()
        if self.process.returncode is None:
            return True

        outs, errs = self.process.communicate()
        self.outs += outs
        self.errs += errs
        return False

class Tester(object):
    def setup_custom_logger(self, name):
        formatter = logging.Formatter(fmt='%(asctime)s %(levelname)-8s %(message)s',
                                      datefmt='%Y-%m-%d %H:%M:%S')
        handler = logging.FileHandler(name, mode='a')
        handler.setFormatter(formatter)
        screen_handler = logging.StreamHandler(stream=sys.stdout)
        screen_handler.setFormatter(formatter)
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.addHandler(screen_handler)
        return logger

    def run_daemon(self, in_cond, in_queue, out_cond, out_queue):
        self.logger = self.setup_custom_logger("tester-%s.log" % (self.name))
        self.logger.info("Started daemon")
        while True:
            in_cond.acquire()
            while in_queue.empty():
                in_cond.wait()

            job = in_queue.get()
            self.Busy = True
            in_cond.release()
            priority = job[0] # Not really used here
            testinfo = job[1]
            workitem = job[2]
            self.logger.info("Got job buildid " + str(workitem.buildnr) + " test " + str(testinfo) )
            result = self.test_worker(testinfo, workitem)
            self.collect_syslogs()
            self.logger.info("Finished job buildid " + str(workitem.buildnr) + " test " + str(testinfo) )
            out_cond.acquire()
            out_queue.put(workitem)
            out_cond.notify()
            out_cond.release()
            self.Busy = False


    def __init__(self, workerinfo, fsinfo, in_cond, in_queue, out_cond, out_queue):
        self.name = workerinfo['name']
        self.serverruncommand = workerinfo['serverrun']
        self.clientruncommand = workerinfo['clientrun']
        self.servernetname = workerinfo['servername']
        self.clientnetname = workerinfo['clientname']
        self.serverarch = workerinfo.get('serverarch', "invalid")
        self.clientarch = workerinfo.get('clientarch', "invalid")
        self.testresultsdir = ""
        self.fsinfo = fsinfo
        self.Busy = False
        self.daemon = threading.Thread(target=self.run_daemon, args=(in_cond, in_queue, out_cond, out_queue))
        self.daemon.daemon = True
        self.daemon.start()

    def collect_syslogs(self):
        for node in [self.servernetname, self.clientnetname]:
            syslogfilename = self.fsinfo["syslogdir"] + "/" + node + ".syslog"

            if not os.path.exists(syslogfilename):
                self.logger.warning("Attempting to collect missing syslog for " + node)
                return
            shutil.copy(syslogfilename, self.testresultsdir + "/")

    def collect_crashdumps(self, node):
        if node.returncode() is None:
            return # It's still alive, so no crashdumps

        crashdirname = self.fsinfo["crashdumps"] + "/" + node.name
        if not os.path.exists(crashdirname):
            return

        if not os.path.isdir(crashdirname):
            self.logger.warning("crashdir location not a dir " + crashdirname)

        outputlocationpathprefix = self.testresultsdir + "/" + node.name + "-"

        # XXX - maybe eventually compress vmcores?
        for crash in os.listdir(crashdirname):
            self.CrashDetected = True
            for item in ["vmcore-dmesg.txt", "vmcore"]:
                filename = crashdirname + "/" + crash + "/" + item
                if os.path.exists(filename):
                    shutil.copy(filename, outputlocationpathprefix + item)
            filename = crashdirname + "/" + crash + "/vmcore.flat"
            if os.path.exists(filename):
                vmcore_flat = open(filename)
                try:
                    result = Popen("makedumpfile -R '" + outputlocationpathprefix + "vmcore'", shell=True, stdin=vmcore_flat, stdout=PIPE, stderr=PIPE)
                except OSError as e:
                    self.logger.warning("Error trying to capture corefile " + str(e))
                else:
                    outs, errs = result.communicate()
                    if result.returncode is not 0:
                        self.logger.warning("Failed processing of core file " + filename + " to " + outputlocationpathprefix + "vmcore with " + outs + " and " + errs)
                    else:
                        try:
                            os.chmod(outputlocationpathprefix + "vmcore", 0644)
                        except OSError:
                            pass # What can we do?

                vmcore_flat.close()

        # Now remove the crash data
        shutil.rmtree(crashdirname)

    def init_new_run(self):
        self.testerrs = ''
        self.testouts = ''
        self.CrashDetected = False
        self.error = False
        # Cleanup old crashdumps and syslogs
        for nodename in [self.servernetname, self.clientnetname]:
            try:
                shutil.rmtree(self.fsinfo["crashdumps"] + "/" + nodename)
            except OSError:
                pass # Not there, who cares
            try:
                with open(self.fsinfo["syslogdir"] + "/" + nodename + ".syslog", 'ab') as f:
                    f.truncate(0)
            except:
                pass # duh, no syslog file yet?

    def test_worker(self, testinfo, workitem,
                    clientdistro="centos7", serverdistro="centos7"):
        self.init_new_run()
        artifactdir = workitem.artifactsdir
        outdir = workitem.testresultsdir

        server = Node(self.servernetname)
        client = Node(self.clientnetname)
        # XXX Incorporate distro into this somehow?
        testname = testinfo.get("test", None)
        if testname is None:
            workitem.UpdateTestStatus(testinfo, "Invalid testinfo!", Failed=True)
            return
        timeout = testinfo.get("timeout", 900)
        fstype = testinfo.get("fstype", "ldiskfs")
        DNE = testinfo.get("DNE", False)
        serverkernel = artifactdir +"/kernel-%s-%s" % (serverdistro, self.serverarch)
        clientkernel = artifactdir +"/kernel-%s-%s" % (clientdistro, self.clientarch)
        serverinitrd = artifactdir +"/initrd-%s-%s.img" % (serverdistro, self.serverarch)
        clientinitrd = artifactdir +"/initrd-%s-%s.img" % (clientdistro, self.clientarch)

        testresultsdir = outdir + "/" + testname + "-" + fstype
        if DNE:
            testresultsdir += "-DNE"

        testresultsdir += "-" + serverdistro + "_" + self.serverarch
        testresultsdir += "-" + clientdistro + "_" + self.clientarch

        clientbuild = artifactdir + "/lustre-" + clientdistro + "-" + self.clientarch + ".ssq"
        serverbuild = artifactdir + "/lustre-" + serverdistro + "-" + self.serverarch + ".ssq"

        if not os.path.exists(serverbuild) or not os.path.exists(clientbuild) or \
           not os.path.exists(serverkernel) or not os.path.exists(serverinitrd) or \
           not os.path.exists(clientkernel) or not os.path.exists(clientinitrd):
            self.logger.error("Our build artifacts are missing?")
            workitem.UpdateTestStatus("Build artifacts missing", Failed=True)

        # Make output test results dir:
        try:
            os.mkdir(testresultsdir)
            # Make it writeable for the tests
            os.chown(testresultsdir, self.fsinfo["testoutputowneruid"], -1)
            # Don't let them delete other files
            os.chmod(testresultsdir, 01755)
        except OSError:
            self.logger.error("Huh, cannot create test results dir")
            workitem.UpdateTestStatus("cannot create test results dir", Failed=True)
            return

        # To know where to copy syslogs and core files
        self.testresultsdir = testresultsdir

        workitem.UpdateTestStatus(testinfo, None, ResultsDir=testresultsdir)

        try:
            server.process = Popen([self.serverruncommand, server.name, serverkernel, serverinitrd, serverbuild, testresultsdir], close_fds=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, universal_newlines=True)
        except (OSError) as details:
            self.logger.warning("Failed to run server " + str(details))
            workitem.UpdateTestStatus(testinfo, "Server startup error", Failed=True)
            return

        try:
            client.process = Popen([self.clientruncommand, client.name, clientkernel, clientinitrd, clientbuild, testresultsdir], close_fds=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, universal_newlines=True)
        except (OSError) as details:
            self.logger.warning("Failed to run client " + str(details))
            server.terminate()
            workitem.UpdateTestStatus(testinfo, "Client startup error", Failed=True)
            return
        # Now we need to wait until both have booted and gave us login prompt
        if server.wait_for_login() is not None:
            client.terminate()
            #pprint(server.errs)
            workitem.UpdateTestStatus(testinfo, "Server died with " + str(server.returncode()), Failed=True)
            return
        if client.wait_for_login() is not None:
            server.terminate()
            #pprint(client.errs)
            workitem.UpdateTestStatus(testinfo, "Client died with " + str(client.returncode()), Failed=True)
            return

        # Now mount NFS in VM
        try:
            command = "ssh -o StrictHostKeyChecking=no root@" + self.clientnetname + \
                    " 'mkdir /tmp/testlogs ; mount 192.168.10.252:/" + \
                    testresultsdir + " /tmp/testlogs -t nfs'"
            args = shlex.split(command)
            setupprocess = Popen(args, close_fds=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, universal_newlines=True)
            outs, errs = setupprocess.communicate(timeout=100) # XXX timeout handling
            self.testouts += outs
            self.testerrs += errs
            if setupprocess.returncode is not 0:
                self.logger.warning("Failed to setup test environment")
                workitem.UpdateTestStatus(testinfo, "Failed to setup test environment", Failed=True, TestStdOut=self.testouts, TestStdErr=self.testerrs)
                server.terminate()
                client.terminate()
                return
        except OSError:
            self.logger.warning("Failed to run test setup " + str(details))
            workitem.UpdateTestStatus(testinfo, "Failed to run test setup " + str(details), Failed=True)
            server.terminate()
            client.terminate()
            return "Testload error"

        try:
            if DNE:
                DNEStr = " MDSDEV2=/dev/vdd MDSCOUNT=2 "
            else:
                DNEStr = " "
            args = ["ssh", "-tt", "-o", "StrictHostKeyChecking=no", "root@" + self.clientnetname,
                    'PDSH="pdsh -S -Rssh -w" mds_HOST=' + self.servernetname +
                    " ost_HOST=" + self.servernetname + " MDSDEV1=/dev/vdc " +
                    "OSTDEV1=/dev/vde OSTDEV2=/dev/vdf LOAD_MODULES_REMOTE=true " +
                    "FSTYPE=" + fstype + DNEStr +
                    "/home/green/git/lustre-release/lustre/tests/auster -D /tmp/testlogs/ -r " + testname ]
            testprocess = Popen(args, close_fds=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, universal_newlines=True)
        except (OSError) as details:
            self.logger.warning("Failed to run test " + str(details))
            workitem.UpdateTestStatus(testinfo, "Failed to run test " + str(details), Failed=True)
            server.terminate()
            client.terminate()
            return "Testload error"


        # XXX Up to this point, if there are any crashdumps, they would not
        # be captured. This is probably fine because Lustre was not involved
        # yet?

        # XXX add a loop here to preiodically test that our servers are alive
        # and also to ensure we don't need to abandon the test for whatever reason
        while testprocess.returncode is None: # XXX add a timer
            try:
                # This is a very ugly workaround to the fact that when you call
                # communicate with timeout, it polls(!!!) the FDs of the subprocess
                # at an insane rate resulting in huge cpu hog. So only let it
                # poll once per call and we do our sleeping ourselves.
                # XXX - perhaps consider doing some sort of a manual select call?
                time.sleep(5) # every 5 seconds, not ideal because that becomes our latency
                outs, errs = testprocess.communicate(timeout=0.01) # cannot have 0 somehow
            except TimeoutExpired:
                self.testouts += outs
                self.testerrs += errs
                if not server.check_node_alive():
                    self.logger.info(server.name + " died while processing test job")
                    self.error = True
                    workitem.UpdateTestStatus(testinfo, "Server crashed", Crash=True)
                    break
                if not client.check_node_alive():
                    self.logger.info(client.name + " died while processing test job")
                    workitem.UpdateTestStatus(testinfo, "Client crashed", Crash=True)
                    self.error = True
                    break
                #self.logger.warning("Job timed out, terminating");
                # self.error = True
                # workitem.UpdateTestStatus(testinfo, "Timeout", Timeout=True)
            else:
                self.testouts += outs
                self.testerrs += errs

        if self.error:
            testprocess.terminate()
            outs, errs = testprocess.communicate()
            self.testouts += outs
            self.testerrs += errs

        self.logger.info("Job finished with code " + str(testprocess.returncode))
        #pprint(self.testerrs)

        # Now kill the client and server
        server.terminate()
        #pprint(souts)
        #pprint(serrs)
        client.terminate()
        #pprint(couts)
        #pprint(cerrs)

        # See if we have any crashdumps
        self.collect_crashdumps(server)
        self.collect_crashdumps(client)

        # If self.error is set that means we already updated the errors state,
        # But we still want them to fall through here to collect the crashdumps
        if not self.error:
            workitem.UpdateTestStatus(testinfo, "Success", Finished=True, Crash=self.CrashDetected, TestStdOut=self.testouts, TestStdErr=self.testerrs)

        return self.testouts
